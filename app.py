import os
import json
import tempfile
import subprocess
import logging
from flask import Flask, request, jsonify
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload, MediaFileUpload
from googleapiclient.errors import HttpError
import io

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

SCOPES = ['https://www.googleapis.com/auth/drive']


def get_drive_service():
    """Build Google Drive service using service account credentials."""
    creds_json = os.environ.get('GOOGLE_SERVICE_ACCOUNT_JSON')
    if not creds_json:
        raise EnvironmentError('GOOGLE_SERVICE_ACCOUNT_JSON env var not set')
    creds_info = json.loads(creds_json)
    credentials = service_account.Credentials.from_service_account_info(
        creds_info, scopes=SCOPES
    )
    return build('drive', 'v3', credentials=credentials)


def download_file(service, file_id, dest_path):
    """Download a file from Google Drive by file_id."""
    request_obj = service.files().get_media(fileId=file_id)
    with open(dest_path, 'wb') as fh:
        downloader = MediaIoBaseDownload(fh, request_obj)
        done = False
        while not done:
            _, done = downloader.next_chunk()


def convert_m4a_to_mp3(input_path, output_path):
    """Convert m4a file to mp3 using FFmpeg."""
    cmd = [
        'ffmpeg', '-y',
        '-i', input_path,
        '-codec:a', 'libmp3lame',
        '-qscale:a', '2',
        output_path
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=240)
    if result.returncode != 0:
        raise RuntimeError(f'FFmpeg error: {result.stderr}')
    return output_path


def upload_file(service, file_path, file_name, folder_id):
    """Upload file to Google Drive folder, return new file id."""
    file_metadata = {
        'name': file_name,
        'parents': [folder_id]
    }
    media = MediaFileUpload(file_path, mimetype='audio/mpeg', resumable=True)
    uploaded = service.files().create(
        body=file_metadata,
        media_body=media,
        fields='id,name'
    ).execute()
    return uploaded


@app.route('/health', methods=['GET'])
def health():
    """Liveness check endpoint."""
    return jsonify({'status': 'ok', 'service': 'ffmpeg-cloudrun-converter'}), 200


@app.route('/convert', methods=['POST'])
def convert():
    """
    Convert a .m4a file from Google Drive to MP3 and upload to target folder.

    Expected JSON body:
    {
        "file_id": "<Google Drive file ID of the .m4a file>",
        "file_name": "<original filename, e.g. call_001.m4a>",
        "target_folder_id": "<Google Drive folder ID to upload MP3 into>"
    }
    """
    data = request.get_json(force=True)
    if not data:
        return jsonify({'error': 'Request body must be JSON'}), 400

    file_id = data.get('file_id')
    file_name = data.get('file_name', 'output.m4a')
    target_folder_id = data.get('target_folder_id')

    if not file_id or not target_folder_id:
        return jsonify({'error': 'file_id and target_folder_id are required'}), 400

    # Derive output MP3 filename
    base_name = os.path.splitext(file_name)[0]
    mp3_name = base_name + '.mp3'

    try:
        service = get_drive_service()

        with tempfile.TemporaryDirectory() as tmpdir:
            m4a_path = os.path.join(tmpdir, file_name)
            mp3_path = os.path.join(tmpdir, mp3_name)

            logger.info(f'Downloading file_id={file_id} name={file_name}')
            download_file(service, file_id, m4a_path)

            logger.info(f'Converting {file_name} -> {mp3_name}')
            convert_m4a_to_mp3(m4a_path, mp3_path)

            logger.info(f'Uploading {mp3_name} to folder {target_folder_id}')
            result = upload_file(service, mp3_path, mp3_name, target_folder_id)

        return jsonify({
            'status': 'success',
            'mp3_file_id': result['id'],
            'mp3_file_name': result['name'],
            'source_file_id': file_id
        }), 200

    except HttpError as e:
        logger.error(f'Google Drive API error: {e}')
        return jsonify({'error': f'Google Drive API error: {str(e)}'}), 500
    except RuntimeError as e:
        logger.error(f'FFmpeg conversion error: {e}')
        return jsonify({'error': str(e)}), 500
    except Exception as e:
        logger.error(f'Unexpected error: {e}')
        return jsonify({'error': f'Internal error: {str(e)}'}), 500


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)
