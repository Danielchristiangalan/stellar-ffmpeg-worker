import os
import uuid
import json
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
    """Core video processing pipeline."""
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

    print(f"Downloading raw video: {raw_key}")
    r2_download_file(r2, raw_key, raw_path)
    r2_download_file(r2, INTRO_PATH, intro_raw_path)
    r2_download_file(r2, OUTRO_PATH, outro_raw_path)

    raw_duration = get_video_duration(raw_path)

    trim_start = float(speech_start) if speech_start is not None else 0.0
    trim_end = float(speech_end) if speech_end is not None else raw_duration
    trim_duration = trim_end - trim_start
    print(f"Trim: {trim_start:.2f}s to {trim_end:.2f}s (duration: {trim_duration:.2f}s)")

    # Pre-process intro
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

    # Pre-process outro
    print("Pre-processing outro")
    subprocess.run([
        'ffmpeg', '-y', '-i', outro_raw_path,
        '-vf', f'fps=30,scale=1920:1080,fade=t=in:st=0:d={FADE_DURATION}',
        '-af', f'aformat=channel_layouts=mono,afade=t=in:st=0:d={FADE_DURATION}',
        '-c:v', 'libx264', '-preset', 'ultrafast', '-crf', '23',
        '-c:a', 'aac', '-ar', '48000',
        outro_path
    ], check=True)

    # PASS 1 — Trim
    print("Pass 1: Trimming to speech start/end")
    subprocess.run([
        'ffmpeg', '-y', '-i', raw_path,
        '-ss', str(trim_start),
        '-t', str(trim_duration),
        '-c:v', 'libx264', '-preset', 'ultrafast', '-crf', '23',
        '-c:a', 'aac', '-ar', '48000',
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

    # Get duration for fade out
    cut_duration = get_video_duration(cut_path)
    fade_out_start = max(0, cut_duration - FADE_DURATION)
    print(f"Cut video duration: {cut_duration:.2f}s, fade out at: {fade_out_start:.2f}s")

    # PASS 2 — Pad to 1920x1080
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

    # PASS 3 — Concat
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

    # Upload video
    print("Uploading final video to R2")
    video_key = f'exports/final_{job_id}.mp4'
    output_url = r2_upload_file(r2, output_path, video_key, 'video/mp4')

    # Upload transcript
    transcript_url = None
    if transcript:
        transcript_path = f'{work_dir}/transcript.txt'
        with open(transcript_path, 'w') as f:
            f.write(transcript)
        transcript_key = f'exports/transcript_{job_id}.txt'
        transcript_url = r2_upload_file(r2, transcript_path, transcript_key, 'text/plain')

    return output_url, transcript_url


@app.route('/health', methods=['GET'])
def health():
    return jsonify({'status': 'ok'})


@app.route('/run', methods=['POST'])
def run():
    """
    Full pipeline: transcribe + process in one call.
    Request body: { "raw_key": "raw-intake/myvideo.mp4", "cuts": [] }
    """
    data = request.get_json()
    raw_key = data.get('raw_key')
    cuts = data.get('cuts', [])

    if not raw_key:
        return jsonify({'status': 'error', 'message': 'raw_key required'}), 400

    job_id = str(uuid.uuid4())
    work_dir = f'/tmp/{job_id}'
    os.makedirs(work_dir, exist_ok=True)

    try:
        r2 = get_r2_client()

        # Step 1 — Transcribe
        print("=== STEP 1: TRANSCRIPTION ===")
        video_path = f'{work_dir}/raw_audio_source.mp4'
        audio_path = f'{work_dir}/audio.wav'

        r2_download_file(r2, raw_key, video_path)
        extract_audio(video_path, audio_path)
        result = transcribe_with_groq(audio_path)

        transcript = result.get('text', '').strip()
        words = result.get('words', [])
        speech_start, speech_end = find_speech_boundaries(words)

        print(f"Transcript length: {len(transcript)} chars")
        print(f"Speech: {speech_start:.2f}s to {speech_end:.2f}s")

        # Clean up audio files to save disk space
        os.remove(video_path)
        os.remove(audio_path)

        # Step 2 — Process video
        print("=== STEP 2: VIDEO PROCESSING ===")
        output_url, transcript_url = run_pipeline(
            r2, raw_key, cuts, transcript,
            speech_start, speech_end,
            job_id, work_dir
        )

        subprocess.run(['rm', '-rf', work_dir])

        return jsonify({
            'status': 'complete',
            'output_url': output_url,
            'transcript_url': transcript_url,
            'transcript': transcript,
            'speech_start': speech_start,
            'speech_end': speech_end,
            'job_id': job_id
        })

    except Exception as e:
        subprocess.run(['rm', '-rf', work_dir])
        print(f"Pipeline error: {e}")
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/transcribe', methods=['POST'])
def transcribe_video():
    """Transcribe only — returns transcript and speech timestamps."""
    data = request.get_json()
    raw_key = data.get('raw_key')

    if not raw_key:
        return jsonify({'status': 'error', 'message': 'raw_key required'}), 400

    job_id = str(uuid.uuid4())
    work_dir = f'/tmp/transcribe_{job_id}'
    os.makedirs(work_dir, exist_ok=True)

    video_path = f'{work_dir}/raw.mp4'
    audio_path = f'{work_dir}/audio.wav'

    try:
        r2 = get_r2_client()
        r2_download_file(r2, raw_key, video_path)
        extract_audio(video_path, audio_path)
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
        print(f"Transcription error: {e}")
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/process', methods=['POST'])
def process_video():
    """Process only — use when you already have transcript and speech timestamps."""
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
        output_url, transcript_url = run_pipeline(
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
        print(f"Pipeline error: {e}")
        return jsonify({'status': 'error', 'message': str(e)}), 500


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port)
