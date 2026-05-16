import os
import uuid
import hashlib
import subprocess
import requests
from flask import Flask, request, jsonify

# Install ffmpeg at runtime if not available
os.system("apt-get update -qq && apt-get install -y ffmpeg -qq")

app = Flask(__name__)

B2_KEY_ID = os.environ.get('B2_APPLICATION_KEY_ID')
B2_APP_KEY = os.environ.get('B2_APPLICATION_KEY')
B2_BUCKET_ID = os.environ.get('B2_BUCKET_ID')
B2_BUCKET_NAME = os.environ.get('B2_BUCKET_NAME')

INTRO_B2_PATH = 'assets/intro.mp4'
OUTRO_B2_PATH = 'assets/outro.mp4'


def b2_authorize():
    r = requests.get(
        'https://api.backblazeb2.com/b2api/v2/b2_authorize_account',
        auth=(B2_KEY_ID, B2_APP_KEY)
    )
    data = r.json()
    print(f"B2 auth response: {data}")
    if 'authorizationToken' not in data:
        raise Exception(f"B2 auth failed: {data}")
    return {
        'token': data['authorizationToken'],
        'api_url': data['apiUrl'],
        'download_url': data['downloadUrl']
    }


def b2_download_file(auth, bucket_name, b2_path, local_path):
    url = f"{auth['download_url']}/file/{bucket_name}/{b2_path}"
    r = requests.get(url, headers={'Authorization': auth['token']}, stream=True)
    with open(local_path, 'wb') as f:
        for chunk in r.iter_content(chunk_size=8192):
            f.write(chunk)


def b2_upload_file(auth, bucket_id, local_path, b2_path, content_type):
    r = requests.post(
        f"{auth['api_url']}/b2api/v2/b2_get_upload_url",
        headers={'Authorization': auth['token']},
        json={'bucketId': bucket_id}
    )
    upload_data = r.json()

    file_size = os.path.getsize(local_path)
    sha1 = hashlib.sha1()
    with open(local_path, 'rb') as f:
        for chunk in iter(lambda: f.read(65536), b''):
            sha1.update(chunk)
    sha1_hex = sha1.hexdigest()

    print(f"Uploading {local_path} ({file_size} bytes) to B2 as {b2_path}")
    with open(local_path, 'rb') as f:
        r = requests.post(
            upload_data['uploadUrl'],
            headers={
                'Authorization': upload_data['authorizationToken'],
                'X-Bz-File-Name': b2_path,
                'Content-Type': content_type,
                'Content-Length': str(file_size),
                'X-Bz-Content-Sha1': sha1_hex
            },
            data=f
        )

    if r.status_code != 200:
        raise Exception(f"B2 upload failed: {r.json()}")

    print(f"Upload complete: {b2_path}")
    return f"{auth['download_url']}/file/{B2_BUCKET_NAME}/{b2_path}"


@app.route('/health', methods=['GET'])
def health():
    return jsonify({'status': 'ok'})


@app.route('/process', methods=['POST'])
def process_video():
    data = request.get_json()
    raw_url = data.get('raw_url')
    cuts = data.get('cuts', [])
    transcript = data.get('transcript', '')

    if not raw_url:
        return jsonify({'status': 'error', 'message': 'raw_url required'}), 400

    job_id = str(uuid.uuid4())
    work_dir = f'/tmp/{job_id}'
    os.makedirs(work_dir, exist_ok=True)

    raw_path = f'{work_dir}/raw.mp4'
    trimmed_path = f'{work_dir}/trimmed.mp4'
    cut_path = f'{work_dir}/cut.mp4'
    padded_path = f'{work_dir}/padded.mp4'
    intro_raw_path = f'{work_dir}/intro_raw.mp4'
    outro_raw_path = f'{work_dir}/outro_raw.mp4'
    intro_path = f'{work_dir}/intro.mp4'
    outro_path = f'{work_dir}/outro.mp4'
    concat_list_path = f'{work_dir}/concat.txt'
    output_path = f'{work_dir}/final.mp4'

    try:
        auth = b2_authorize()
        bucket_id = B2_BUCKET_ID

        # Download raw video with auth
        print(f"Downloading raw video from {raw_url}")
        r = requests.get(raw_url, headers={'Authorization': auth['token']}, stream=True)
        with open(raw_path, 'wb') as f:
            for chunk in r.iter_content(chunk_size=8192):
                f.write(chunk)

        # Download intro and outro
        print("Downloading intro and outro")
        b2_download_file(auth, B2_BUCKET_NAME, INTRO_B2_PATH, intro_raw_path)
        b2_download_file(auth, B2_BUCKET_NAME, OUTRO_B2_PATH, outro_raw_path)

        # Pre-process intro: normalize to 30fps, mono audio, 1920x1080, fade out
        print("Pre-processing intro")
        subprocess.run([
            'ffmpeg', '-y', '-i', intro_raw_path,
            '-vf', 'fps=30,scale=1920:1080,fade=t=out:st=3.62:d=0.5',
            '-af', 'aformat=channel_layouts=mono,afade=t=out:st=3.62:d=0.5',
            '-c:v', 'libx264', '-preset', 'ultrafast', '-crf', '23',
            '-c:a', 'aac', '-ar', '48000',
            intro_path
        ], check=True)

        # Pre-process outro: normalize to 30fps, mono audio, 1920x1080, fade in
        print("Pre-processing outro")
        subprocess.run([
            'ffmpeg', '-y', '-i', outro_raw_path,
            '-vf', 'fps=30,scale=1920:1080,fade=t=in:st=0:d=0.5',
            '-af', 'aformat=channel_layouts=mono,afade=t=in:st=0:d=0.5',
            '-c:v', 'libx264', '-preset', 'ultrafast', '-crf', '23',
            '-c:a', 'aac', '-ar', '48000',
            outro_path
        ], check=True)

        # PASS 1 — Trim silence from start using copy (fast, no re-encode)
        print("Pass 1: Trimming silence from start")
        subprocess.run([
            'ffmpeg', '-y', '-i', raw_path,
            '-ss', '0.5',
            '-c', 'copy',
            trimmed_path
        ], check=True)

        # PASS 1b — Apply cuts
        if cuts:
            print(f"Pass 1b: Applying {len(cuts)} cuts")
            select_expr = '+'.join([
                f"between(t,{c['in']},{c['out']})" for c in cuts
            ])
            filter_complex = (
                f"[0:v]select='not({select_expr})',setpts=N/FRAME_RATE/TB[v];"
                f"[0:a]aselect='not({select_expr})',asetpts=N/SR/TB[a]"
            )
            subprocess.run([
                'ffmpeg', '-y', '-i', trimmed_path,
                '-filter_complex', filter_complex,
                '-map', '[v]', '-map', '[a]',
                '-c:v', 'libx264', '-preset', 'ultrafast', '-crf', '23',
                '-c:a', 'aac', '-ar', '48000',
                cut_path
            ], check=True)
        else:
            cut_path = trimmed_path

        # PASS 2 — Pad to 1920x1080, normalize to 30fps mono, fade in/out
        print("Pass 2: Padding to 1920x1080")
        subprocess.run([
            'ffmpeg', '-y', '-i', cut_path,
            '-vf', (
                'scale=1720:968:force_original_aspect_ratio=decrease,'
                'pad=1920:1080:(ow-iw)/2:(oh-ih)/2:white,'
                'fps=30,'
                'fade=t=in:st=0:d=0.5,'
                'fade=t=out:st=287:d=0.5'
            ),
            '-af', 'aformat=channel_layouts=mono,afade=t=in:st=0:d=0.5',
            '-c:v', 'libx264', '-preset', 'ultrafast', '-crf', '23',
            '-c:a', 'aac', '-ar', '48000',
            padded_path
        ], check=True)

        # PASS 3 — Concat intro + main + outro, re-encode to fix audio sync
        print("Pass 3: Concatenating intro + main + outro")
        with open(concat_list_path, 'w') as f:
            f.write(f"file '{intro_path}'\n")
            f.write(f"file '{padded_path}'\n")
            f.write(f"file '{outro_path}'\n")

        subprocess.run([
            'ffmpeg', '-y',
            '-f', 'concat', '-safe', '0',
            '-i', concat_list_path,
            '-c:v', 'libx264', '-preset', 'ultrafast', '-crf', '23',
            '-c:a', 'aac', '-ar', '48000',
            '-async', '1',
            output_path
        ], check=True)

        # Upload final video to B2 (streaming)
        print("Uploading final video to B2")
        video_b2_path = f'exports/final_{job_id}.mp4'
        output_url = b2_upload_file(auth, bucket_id, output_path, video_b2_path, 'video/mp4')

        # Upload transcript if provided
        transcript_url = None
        if transcript:
            transcript_path = f'{work_dir}/transcript.txt'
            with open(transcript_path, 'w') as f:
                f.write(transcript)
            transcript_b2_path = f'exports/transcript_{job_id}.txt'
            transcript_url = b2_upload_file(
                auth, bucket_id, transcript_path, transcript_b2_path, 'text/plain'
            )

        # Cleanup
        subprocess.run(['rm', '-rf', work_dir])

        return jsonify({
            'status': 'complete',
            'output_url': output_url,
            'transcript_url': transcript_url,
            'job_id': job_id
        })

    except Exception as e:
        subprocess.run(['rm', '-rf', work_dir])
        print(f"Pipeline error: {e}")
        return jsonify({'status': 'error', 'message': str(e)}), 500


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port)
