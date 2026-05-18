import os
import uuid
import json
import threading
import subprocess
import boto3
from botocore.config import Config
from flask import Flask, request, jsonify
import requests as http_requests

# Install ffmpeg at runtime if not available
os.system("apt-get update -qq && apt-get install -y ffmpeg -qq")

app = Flask(__name__)

R2_ACCESS_KEY_ID = os.environ.get('R2_ACCESS_KEY_ID')
R2_SECRET_ACCESS_KEY = os.environ.get('R2_SECRET_ACCESS_KEY')
R2_ACCOUNT_ID = os.environ.get('R2_ACCOUNT_ID')
R2_BUCKET_NAME = os.environ.get('R2_BUCKET_NAME')
GROQ_API_KEY = os.environ.get('GROQ_API_KEY')

INTRO_PATH = 'assets/intro.mp4'
OUTRO_PATH = 'assets/outro.mp4'

INTRO_DURATION = 4.12
FADE_DURATION = 0.5
WORD_MAX_DURATION = 3.0
EXPORT_PREFIX = 'Acumatica How To_'

# In-memory job store
jobs = {}


def get_r2_client():
    return boto3.client(
        's3',
        endpoint_url=f'https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com',
        aws_access_key_id=R2_ACCESS_KEY_ID,
        aws_secret_access_key=R2_SECRET_ACCESS_KEY,
        config=Config(signature_version='s3v4'),
        region_name='auto'
    )


def r2_download_file(r2, key, local_path):
    print(f"Downloading {key} from R2")
    r2.download_file(R2_BUCKET_NAME, key, local_path)
    print(f"Downloaded {key}")


def r2_upload_file(r2, local_path, key, content_type):
    file_size = os.path.getsize(local_path)
    print(f"Uploading {local_path} ({file_size} bytes) to R2 as {key}")
    r2.upload_file(
        local_path,
        R2_BUCKET_NAME,
        key,
        ExtraArgs={'ContentType': content_type}
    )
    print(f"Upload complete: {key}")
    return f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com/{R2_BUCKET_NAME}/{key}"


def r2_upload_string(r2, content, key, content_type):
    print(f"Uploading string to R2 as {key}")
    r2.put_object(
        Bucket=R2_BUCKET_NAME,
        Key=key,
        Body=content.encode('utf-8'),
        ContentType=content_type
    )
    print(f"Upload complete: {key}")
    return f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com/{R2_BUCKET_NAME}/{key}"


def get_video_duration(path):
    result = subprocess.run([
        'ffprobe', '-v', 'quiet',
        '-print_format', 'json',
        '-show_format',
        path
    ], capture_output=True, text=True, check=True)
    info = json.loads(result.stdout)
    duration = float(info['format']['duration'])
    print(f"Video duration: {duration:.2f}s")
    return duration


def convert_to_mp4(input_path, output_path):
    """Convert non-MP4 video to MP4."""
    print(f"Converting to MP4: {input_path} -> {output_path}")
    subprocess.run([
        'ffmpeg', '-y', '-i', input_path,
        '-c:v', 'libx264', '-preset', 'ultrafast', '-crf', '23',
        '-c:a', 'aac', '-ar', '48000',
        output_path
    ], check=True)
    print("Conversion complete")


def ensure_mp4(input_path, work_dir, prefix='converted'):
    """Return an MP4 path — converts if needed, returns original if already MP4."""
    ext = os.path.splitext(input_path)[1].lower()
    if ext == '.mp4':
        print(f"Already MP4, no conversion needed")
        return input_path
    output_path = f'{work_dir}/{prefix}.mp4'
    convert_to_mp4(input_path, output_path)
    return output_path


def extract_audio(video_path, audio_path):
    subprocess.run([
        'ffmpeg', '-y', '-i', video_path,
        '-vn', '-ar', '16000', '-ac', '1',
        '-c:a', 'pcm_s16le',
        audio_path
    ], check=True)
    print(f"Audio extracted to {audio_path}")


def transcribe_with_groq(audio_path):
    print("Sending audio to Groq Whisper...")
    with open(audio_path, 'rb') as f:
        response = http_requests.post(
            'https://api.groq.com/openai/v1/audio/transcriptions',
            headers={'Authorization': f'Bearer {GROQ_API_KEY}'},
            files={'file': ('audio.wav', f, 'audio/wav')},
            data={
                'model': 'whisper-large-v3',
                'response_format': 'verbose_json',
                'timestamp_granularities[]': 'word'
            }
        )
    if response.status_code != 200:
        raise Exception(f"Groq transcription failed: {response.text}")
    print("Transcription complete")
    return response.json()


def find_speech_boundaries(words):
    if not words:
        return None, None

    normal_words = [
        w for w in words
        if (w.get('end', 0) - w.get('start', 0)) <= WORD_MAX_DURATION
    ]

    print(f"Total words: {len(words)}, Normal words: {len(normal_words)}")

    if not normal_words:
        return words[0].get('start', 0.0), words[-1].get('end', 0.0)

    speech_start = normal_words[0].get('start', 0.0)
    speech_end = normal_words[-1].get('end', 0.0)

    print(f"First normal word: '{normal_words[0].get('word')}' at {speech_start:.2f}s")
    print(f"Last normal word: '{normal_words[-1].get('word')}' at {speech_end:.2f}s")
    print(f"Speech boundaries: {speech_start:.2f}s to {speech_end:.2f}s")

    return speech_start, speech_end


def run_pipeline(r2, raw_key, cuts, transcript, speech_start, speech_end, job_id, work_dir):
    ext = os.path.splitext(raw_key)[1].lower() or '.mp4'
    raw_path = f'{work_dir}/raw_original{ext}'
    trimmed_path = f'{work_dir}/trimmed.mp4'
    cut_path = f'{work_dir}/cut.mp4'
    padded_path = f'{work_dir}/padded.mp4'
    intro_raw_path = f'{work_dir}/intro_raw.mp4'
    outro_raw_path = f'{work_dir}/outro_raw.mp4'
    intro_path = f'{work_dir}/intro.mp4'
    outro_path = f'{work_dir}/outro.mp4'
    concat_list_path = f'{work_dir}/concat.txt'
    output_path = f'{work_dir}/final.mp4'

    print(f"Downloading raw video: {raw_key}")
    r2_download_file(r2, raw_key, raw_path)
    r2_download_file(r2, INTRO_PATH, intro_raw_path)
    r2_download_file(r2, OUTRO_PATH, outro_raw_path)

    # Convert to MP4 if needed
    converted_path = ensure_mp4(raw_path, work_dir, prefix='raw_converted')

    raw_duration = get_video_duration(converted_path)

    trim_start = float(speech_start) if speech_start is not None else 0.0
    trim_end = float(speech_end) if speech_end is not None else raw_duration
    trim_duration = trim_end - trim_start
    print(f"Trim: {trim_start:.2f}s to {trim_end:.2f}s (duration: {trim_duration:.2f}s)")

    print("Pre-processing intro")
    intro_fade_start = INTRO_DURATION - FADE_DURATION
    subprocess.run([
        'ffmpeg', '-y', '-i', intro_raw_path,
        '-vf', f'fps=30,scale=1920:1080,fade=t=out:st={intro_fade_start}:d={FADE_DURATION}',
        '-af', f'aformat=channel_layouts=mono,afade=t=out:st={intro_fade_start}:d={FADE_DURATION}',
        '-c:v', 'libx264', '-preset', 'ultrafast', '-crf', '23',
        '-c:a', 'aac', '-ar', '48000',
        intro_path
    ], check=True)

    print("Pre-processing outro")
    subprocess.run([
        'ffmpeg', '-y', '-i', outro_raw_path,
        '-vf', f'fps=30,scale=1920:1080,fade=t=in:st=0:d={FADE_DURATION}',
        '-af', f'aformat=channel_layouts=mono,afade=t=in:st=0:d={FADE_DURATION}',
        '-c:v', 'libx264', '-preset', 'ultrafast', '-crf', '23',
        '-c:a', 'aac', '-ar', '48000',
        outro_path
    ], check=True)

    print("Pass 1: Trimming to speech start/end")
    subprocess.run([
        'ffmpeg', '-y', '-i', converted_path,
        '-ss', str(trim_start),
        '-t', str(trim_duration),
        '-c:v', 'libx264', '-preset', 'ultrafast', '-crf', '23',
        '-c:a', 'aac', '-ar', '48000',
        trimmed_path
    ], check=True)

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

    cut_duration = get_video_duration(cut_path)
    fade_out_start = max(0, cut_duration - FADE_DURATION)

    print("Pass 2: Padding to 1920x1080")
    subprocess.run([
        'ffmpeg', '-y', '-i', cut_path,
        '-vf', (
            f'scale=1720:968:force_original_aspect_ratio=decrease,'
            f'pad=1920:1080:(ow-iw)/2:(oh-ih)/2:white,'
            f'fps=30,'
            f'fade=t=in:st=0:d={FADE_DURATION},'
            f'fade=t=out:st={fade_out_start}:d={FADE_DURATION}'
        ),
        '-af', (
            f'aformat=channel_layouts=mono,'
            f'afade=t=in:st=0:d={FADE_DURATION},'
            f'afade=t=out:st={fade_out_start}:d={FADE_DURATION}'
        ),
        '-c:v', 'libx264', '-preset', 'ultrafast', '-crf', '23',
        '-c:a', 'aac', '-ar', '48000',
        padded_path
    ], check=True)

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
        '-vsync', 'cfr', '-async', '1',
        output_path
    ], check=True)

    raw_filename = os.path.splitext(os.path.basename(raw_key))[0]
    video_key = f'exports/{EXPORT_PREFIX}{raw_filename}.mp4'
    print(f"Export filename: {video_key}")
    output_url = r2_upload_file(r2, output_path, video_key, 'video/mp4')

    transcript_url = None
    if transcript:
        transcript_key = f'exports/{EXPORT_PREFIX}{raw_filename}.txt'
        transcript_url = r2_upload_string(r2, transcript, transcript_key, 'text/plain')

    return output_url, transcript_url, raw_filename


def process_job(job_id, raw_key, cuts):
    work_dir = f'/tmp/{job_id}'
    os.makedirs(work_dir, exist_ok=True)

    try:
        jobs[job_id]['status'] = 'transcribing'
        r2 = get_r2_client()

        print(f"[{job_id}] === STEP 1: TRANSCRIPTION ===")
        ext = os.path.splitext(raw_key)[1].lower() or '.mp4'
        video_path = f'{work_dir}/raw_audio_source{ext}'
        audio_path = f'{work_dir}/audio.wav'

        r2_download_file(r2, raw_key, video_path)

        # Convert to MP4 only if needed
        mp4_path = ensure_mp4(video_path, work_dir, prefix='audio_source_converted')

        extract_audio(mp4_path, audio_path)
        result = transcribe_with_groq(audio_path)

        transcript = result.get('text', '').strip()
        words = result.get('words', [])
        speech_start, speech_end = find_speech_boundaries(words)

        # Clean up audio source files
        os.remove(video_path)
        if mp4_path != video_path and os.path.exists(mp4_path):
            os.remove(mp4_path)
        os.remove(audio_path)

        jobs[job_id]['status'] = 'processing'
        jobs[job_id]['transcript'] = transcript
        jobs[job_id]['speech_start'] = speech_start
        jobs[job_id]['speech_end'] = speech_end
        jobs[job_id]['raw_key'] = raw_key

        print(f"[{job_id}] === STEP 2: VIDEO PROCESSING ===")
        output_url, transcript_url, raw_filename = run_pipeline(
            r2, raw_key, cuts, transcript,
            speech_start, speech_end,
            job_id, work_dir
        )

        subprocess.run(['rm', '-rf', work_dir])

        jobs[job_id].update({
            'status': 'complete',
            'output_url': output_url,
            'transcript_url': transcript_url,
            'raw_key': raw_key,
            'raw_filename': raw_filename,
        })
        print(f"[{job_id}] Job complete")

    except Exception as e:
        subprocess.run(['rm', '-rf', work_dir])
        jobs[job_id].update({
            'status': 'error',
            'error': str(e)
        })
        print(f"[{job_id}] Job failed: {e}")


@app.route('/health', methods=['GET'])
def health():
    return jsonify({'status': 'ok'})


@app.route('/run', methods=['POST'])
def run():
    data = request.get_json()
    raw_key = data.get('raw_key')
    cuts = data.get('cuts', [])

    if not raw_key:
        return jsonify({'status': 'error', 'message': 'raw_key required'}), 400

    job_id = str(uuid.uuid4())
    jobs[job_id] = {
        'status': 'queued',
        'raw_key': raw_key,
        'job_id': job_id
    }

    thread = threading.Thread(target=process_job, args=(job_id, raw_key, cuts))
    thread.daemon = True
    thread.start()

    return jsonify({
        'status': 'queued',
        'job_id': job_id,
        'message': f'Job started. Poll /status/{job_id} for updates.'
    })


@app.route('/status/<job_id>', methods=['GET'])
def status(job_id):
    if job_id not in jobs:
        return jsonify({'status': 'error', 'message': 'Job not found'}), 404
    return jsonify(jobs[job_id])


@app.route('/save-thumbnail', methods=['POST'])
def save_thumbnail():
    data = request.get_json()
    r2_key = data.get('r2_key')
    content = data.get('content')

    if not r2_key or not content:
        return jsonify({'status': 'error', 'message': 'r2_key and content required'}), 400

    try:
        r2 = get_r2_client()
        url = r2_upload_string(r2, content, r2_key, 'text/plain')
        return jsonify({'status': 'complete', 'url': url})

    except Exception as e:
        print(f"Save thumbnail error: {e}")
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/transcribe', methods=['POST'])
def transcribe_video():
    data = request.get_json()
    raw_key = data.get('raw_key')

    if not raw_key:
        return jsonify({'status': 'error', 'message': 'raw_key required'}), 400

    job_id = str(uuid.uuid4())
    work_dir = f'/tmp/transcribe_{job_id}'
    os.makedirs(work_dir, exist_ok=True)

    ext = os.path.splitext(raw_key)[1].lower() or '.mp4'
    video_path = f'{work_dir}/raw{ext}'
    audio_path = f'{work_dir}/audio.wav'

    try:
        r2 = get_r2_client()
        r2_download_file(r2, raw_key, video_path)
        mp4_path = ensure_mp4(video_path, work_dir, prefix='transcribe_converted')
        extract_audio(mp4_path, audio_path)
        result = transcribe_with_groq(audio_path)

        transcript = result.get('text', '').strip()
        words = result.get('words', [])
        speech_start, speech_end = find_speech_boundaries(words)

        subprocess.run(['rm', '-rf', work_dir])

        return jsonify({
            'status': 'complete',
            'transcript': transcript,
            'speech_start': speech_start,
            'speech_end': speech_end,
            'raw_key': raw_key
        })

    except Exception as e:
        subprocess.run(['rm', '-rf', work_dir])
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/process', methods=['POST'])
def process_video():
    data = request.get_json()
    raw_key = data.get('raw_key')
    cuts = data.get('cuts', [])
    transcript = data.get('transcript', '')
    speech_start = data.get('speech_start', None)
    speech_end = data.get('speech_end', None)

    if not raw_key:
        return jsonify({'status': 'error', 'message': 'raw_key required'}), 400

    job_id = str(uuid.uuid4())
    work_dir = f'/tmp/{job_id}'
    os.makedirs(work_dir, exist_ok=True)

    try:
        r2 = get_r2_client()
        output_url, transcript_url, _ = run_pipeline(
            r2, raw_key, cuts, transcript,
            speech_start, speech_end,
            job_id, work_dir
        )

        subprocess.run(['rm', '-rf', work_dir])

        return jsonify({
            'status': 'complete',
            'output_url': output_url,
            'transcript_url': transcript_url,
            'job_id': job_id
        })

    except Exception as e:
        subprocess.run(['rm', '-rf', work_dir])
        return jsonify({'status': 'error', 'message': str(e)}), 500


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port)
