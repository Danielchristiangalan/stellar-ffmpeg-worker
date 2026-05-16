python
from flask import Flask, request, jsonify
import subprocess
import os
import urllib.request
import uuid
import requests
import hashlib

app = Flask(__name__)

B2_KEY_ID = os.environ.get('B2_APPLICATION_KEY_ID')
B2_APP_KEY = os.environ.get('B2_APPLICATION_KEY')
B2_BUCKET_NAME = os.environ.get('B2_BUCKET_NAME')
B2_BUCKET_ID = os.environ.get('B2_BUCKET_ID')

def b2_authorize():
    r = requests.get(
        'https://api.backblazeb2.com/b2api/v2/b2_authorize_account',
        auth=(B2_KEY_ID, B2_APP_KEY)
    )
    data = r.json()
    return {
        'token': data['authorizationToken'],
        'api_url': data['apiUrl'],
        'download_url': data['downloadUrl']
    }

def b2_get_upload_url(auth):
    r = requests.post(
        f"{auth['api_url']}/b2api/v2/b2_get_upload_url",
        headers={'Authorization': auth['token']},
        json={'bucketId': B2_BUCKET_ID}
    )
    return r.json()

def b2_upload_file(auth, file_path, b2_path, content_type):
    upload_data = b2_get_upload_url(auth)
    with open(file_path, 'rb') as f:
        file_data = f.read()
    sha1 = hashlib.sha1(file_data).hexdigest()
    r = requests.post(
        upload_data['uploadUrl'],
        headers={
            'Authorization': upload_data['authorizationToken'],
            'X-Bz-File-Name': b2_path,
            'Content-Type': content_type,
            'Content-Length': str(len(file_data)),
            'X-Bz-Content-Sha1': sha1
        },
        data=file_data
    )
    file_id = r.json()['fileId']
    download_url = f"{auth['download_url']}/b2api/v2/b2_download_file_by_id?fileId={file_id}"
    return download_url

def b2_get_download_url(auth, b2_path):
    return f"{auth['download_url']}/file/{B2_BUCKET_NAME}/{b2_path}"

def time_to_seconds(t):
    parts = t.split(':')
    return float(parts[0]) * 3600 + float(parts[1]) * 60 + float(parts[2])

@app.route('/process', methods=['POST'])
def process_video():
    data = request.json
    raw_url = data['raw_url']
    cuts = data.get('cuts', [])
    transcript = data.get('transcript', '')

    job_id = str(uuid.uuid4())
    work_dir = f'/tmp/{job_id}'
    os.makedirs(work_dir, exist_ok=True)

    raw_path = f'{work_dir}/raw.mp4'
    intro_path = f'{work_dir}/intro.mp4'
    outro_path = f'{work_dir}/outro.mp4'
    trimmed_path = f'{work_dir}/trimmed.mp4'
    cut_path = f'{work_dir}/cut.mp4'
    padded_path = f'{work_dir}/padded.mp4'
    with_intro_path = f'{work_dir}/with_intro.mp4'
    output_path = f'{work_dir}/final.mp4'

    # Authorize B2
    auth = b2_authorize()

    # Get intro and outro download URLs
    intro_url = b2_get_download_url(auth, 'assets/intro.mp4')
    outro_url = b2_get_download_url(auth, 'assets/outro.mp4')

    # Download all files
    urllib.request.urlretrieve(raw_url, raw_path)
    urllib.request.urlretrieve(intro_url, intro_path)
    urllib.request.urlretrieve(outro_url, outro_path)

    # Step 1 — Remove silence at start and end
    subprocess.run([
        'ffmpeg', '-i', raw_path,
        '-ss', '0.5',
        '-c:v', 'libx264', '-c:a', 'aac',
        trimmed_path
    ], check=True)

    # Step 2 — Apply AI cuts
    if cuts:
        select_parts = []
        for cut in cuts:
            t_in = time_to_seconds(cut['in'])
            t_out = time_to_seconds(cut['out'])
            select_parts.append(f"between(t,{t_in},{t_out})")
        select_expr = '+'.join(select_parts)
        filter_complex = (
            f"[0:v]select='not({select_expr})',setpts=N/FRAME_RATE/TB[v];"
            f"[0:a]aselect='not({select_expr})',asetpts=N/SR/TB[a]"
        )
        subprocess.run([
            'ffmpeg', '-i', trimmed_path,
            '-filter_complex', filter_complex,
            '-map', '[v]', '-map', '[a]',
            '-c:v', 'libx264', '-c:a', 'aac',
            cut_path
        ], check=True)
    else:
        cut_path = trimmed_path

    # Step 3 — Add white background with margin
    subprocess.run([
        'ffmpeg', '-i', cut_path,
        '-vf', (
            'scale=1720:968:force_original_aspect_ratio=decrease,'
            'pad=1920:1080:(ow-iw)/2:(oh-ih)/2:white'
        ),
        '-c:v', 'libx264', '-c:a', 'aac',
        padded_path
    ], check=True)

    # Step 4 — Crossfade intro + padded video
    subprocess.run([
        'ffmpeg',
        '-i', intro_path,
        '-i', padded_path,
        '-filter_complex',
        '[0:v][1:v]xfade=transition=fade:duration=0.5:offset=duration-0.5[vout];'
        '[0:a][1:a]acrossfade=d=0.5[aout]',
        '-map', '[vout]', '-map', '[aout]',
        '-c:v', 'libx264', '-c:a', 'aac',
        with_intro_path
    ], check=True)

    # Step 5 — Crossfade with outro
    subprocess.run([
        'ffmpeg',
        '-i', with_intro_path,
        '-i', outro_path,
        '-filter_complex',
        '[0:v][1:v]xfade=transition=fade:duration=0.5:offset=duration-0.5[vout];'
        '[0:a][1:a]acrossfade=d=0.5[aout]',
        '-map', '[vout]', '-map', '[aout]',
        '-c:v', 'libx264', '-c:a', 'aac',
        output_path
    ], check=True)

    # Upload final video to B2 exports
    video_b2_path = f'exports/final_{job_id}.mp4'
    output_url = b2_upload_file(auth, output_path, video_b2_path, 'video/mp4')

    # Upload transcript to B2 exports
    transcript_url = None
    if transcript:
        transcript_path = f'{work_dir}/transcript.txt'
        with open(transcript_path, 'w') as f:
            f.write(transcript)
        transcript_b2_path = f'exports/transcript_{job_id}.txt'
        transcript_url = b2_upload_file(
            auth, transcript_path, transcript_b2_path, 'text/plain'
        )

    # Cleanup
    subprocess.run(['rm', '-rf', work_dir])

    return jsonify({
        'status': 'complete',
        'output_url': output_url,
        'transcript_url': transcript_url,
        'job_id': job_id
    })

@app.route('/health', methods=['GET'])
def health():
    return jsonify({'status': 'ok'})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=10000)
