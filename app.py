import os
import re
import uuid
import json
import hashlib
import subprocess
import requests
import boto3
from botocore.config import Config
from flask import Flask, request, jsonify

# Install ffmpeg at runtime if not available
os.system("apt-get update -qq && apt-get install -y ffmpeg -qq")

app = Flask(__name__)

R2_ACCESS_KEY_ID = os.environ.get('R2_ACCESS_KEY_ID')
R2_SECRET_ACCESS_KEY = os.environ.get('R2_SECRET_ACCESS_KEY')
R2_ACCOUNT_ID = os.environ.get('R2_ACCOUNT_ID')
R2_BUCKET_NAME = os.environ.get('R2_BUCKET_NAME')

INTRO_PATH = 'assets/intro.mp4'
OUTRO_PATH = 'assets/outro.mp4'

# Intro/outro durations for fade processing
INTRO_DURATION = 4.12
OUTRO_DURATION = 4.0
FADE_DURATION = 0.5


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


def get_video_duration(path):
    """Get video duration in seconds using ffprobe."""
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


def detect_silence(path, noise_threshold=-40, min_duration=0.3):
    """Detect silence periods in video. Returns list of (start, end) tuples."""
    result = subprocess.run([
        'ffmpeg', '-i', path,
        '-af', f'silencedetect=noise={noise_threshold}dB:d={min_duration}',
        '-f', 'null', '-'
    ], capture_output=True, text=True)

    silence_periods = []
    silence_start = None

    for line in result.stderr.split('\n'):
        if 'silence_start' in line:
            match = re.search(r'silence_start: ([\d.]+)', line)
            if match:
                silence_start = float(match.group(1))
        if 'silence_end' in line and silence_start is not None:
            match = re.search(r'silence_end: ([\d.]+)', line)
            if match:
                silence_end = float(match.group(1))
                silence_periods.append((silence_start, silence_end))
                silence_start = None

    return silence_periods


def get_trim_points(path, duration):
    """Auto-detect start and end trim points based on silence."""
    silence = detect_silence(path)
    print(f"Detected silence periods: {silence}")

    # Default: no trim
    trim_start = 0.0
    trim_end = duration

    # Trim leading silence
    if silence and silence[0][0] < 1.0:
        trim_start = silence[0][1]
        print(f"Trimming {trim_start:.2f}s of leading silence")

    # Trim trailing silence
    if silence and silence[-1][1] > duration - 2.0:
        trim_end = silence[-1][0]
        print(f"Trimming trailing silence, new end: {trim_end:.2f}s")

    return trim_start, trim_end


@app.route('/health', methods=['GET'])
def health():
    return jsonify({'status': 'ok'})


@app.route('/process', methods=['POST'])
def process_video():
    data = request.get_json()
    raw_key = data.get('raw_key')
    cuts = data.get('cuts', [])
    transcript = data.get('transcript', '')

    if not raw_key:
        return jsonify({'status': 'error', 'message': 'raw_key required'}), 400

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
        r2 = get_r2_client()

        # Download all files from R2
        print(f"Downloading raw video: {raw_key}")
        r2_download_file(r2, raw_key, raw_path)
        r2_download_file(r2, INTRO_PATH, intro_raw_path)
        r2_download_file(r2, OUTRO_PATH, outro_raw_path)

        # Pre-process intro: normalize to 30fps, mono, 1920x1080, fade out
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

        # Pre-process outro: normalize to 30fps, mono, 1920x1080, fade in
        print("Pre-processing outro")
        subprocess.run([
            'ffmpeg', '-y', '-i', outro_raw_path,
            '-vf', f'fps=30,scale=1920:1080,fade=t=in:st=0:d={FADE_DURATION}',
            '-af', f'aformat=channel_layouts=mono,afade=t=in:st=0:d={FADE_DURATION}',
            '-c:v', 'libx264', '-preset', 'ultrafast', '-crf', '23',
            '-c:a', 'aac', '-ar', '48000',
            outro_path
        ], check=True)

        # Auto-detect silence and get trim points
        print("Detecting silence for auto-trim")
        raw_duration = get_video_duration(raw_path)
        trim_start, trim_end = get_trim_points(raw_path, raw_duration)
        trim_duration = trim_end - trim_start
        print(f"Trim: {trim_start:.2f}s to {trim_end:.2f}s (duration: {trim_duration:.2f}s)")

        # PASS 1 — Trim silence using detected points
        print("Pass 1: Trimming silence")
        subprocess.run([
            'ffmpeg', '-y', '-i', raw_path,
            '-ss', str(trim_start),
            '-t', str(trim_duration),
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

        # Get duration of cut video for fade out timing
        cut_duration = get_video_duration(cut_path)
        fade_out_start = cut_duration - FADE_DURATION
        print(f"Cut video duration: {cut_duration:.2f}s, fade out at: {fade_out_start:.2f}s")

        # PASS 2 — Pad to 1920x1080, normalize to 30fps mono, auto fade in/out
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

        # PASS 3 — Concat intro + main + outro, re-encode for audio sync
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
            '-vsync', '1', '-async', '1',
            output_path
        ], check=True)

        # Upload final video to R2
        print("Uploading final video to R2")
        video_key = f'exports/final_{job_id}.mp4'
        output_url = r2_upload_file(r2, output_path, video_key, 'video/mp4')

        # Upload transcript if provided
        transcript_url = None
        if transcript:
            transcript_path = f'{work_dir}/transcript.txt'
            with open(transcript_path, 'w') as f:
                f.write(transcript)
            transcript_key = f'exports/transcript_{job_id}.txt'
            transcript_url = r2_upload_file(r2, transcript_path, transcript_key, 'text/plain')

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
