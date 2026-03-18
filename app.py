import os
import json
import tempfile
import subprocess
import logging
import threading
import uuid
import datetime
from flask import Flask, request, jsonify
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload, MediaFileUpload
from googleapiclient.errors import HttpError
import io
import requests

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

SCOPES = ['https://www.googleapis.com/auth/drive']
_processed_ids = set()
_processing_lock = threading.Lock()

# Store the latest pageToken for changes API
_changes_page_token = None
_page_token_lock = threading.Lock()


def get_drive_service():
    creds_json = os.environ.get('GOOGLE_SERVICE_ACCOUNT_JSON')
    if not creds_json:
        raise EnvironmentError('GOOGLE_SERVICE_ACCOUNT_JSON env var not set')
    creds_info = json.loads(creds_json)
    credentials = service_account.Credentials.from_service_account_info(
        creds_info, scopes=SCOPES
    )
    return build('drive', 'v3', credentials=credentials)


def download_file(service, file_id, dest_path):
    request_obj = service.files().get_media(fileId=file_id, supportsAllDrives=True)
    with open(dest_path, 'wb') as fh:
        downloader = MediaIoBaseDownload(fh, request_obj)
        done = False
        while not done:
            _, done = downloader.next_chunk()


def convert_m4a_to_mp3(input_path, output_path):
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
    file_metadata = {
        'name': file_name,
        'parents': [folder_id]
    }
    media = MediaFileUpload(file_path, mimetype='audio/mpeg', resumable=True)
    uploaded = service.files().create(
        body=file_metadata,
        media_body=media,
        fields='id,name',
        supportsAllDrives=True
    ).execute()
    return uploaded


def list_files_in_folder(service, folder_id, name_suffix=None):
    results = []
    page_token = None
    query = f"'{folder_id}' in parents and trashed=false"
    while True:
        resp = service.files().list(
            q=query,
            fields='nextPageToken, files(id, name, createdTime)',
            pageToken=page_token,
            pageSize=1000,
            orderBy='createdTime desc',
            supportsAllDrives=True,
            includeItemsFromAllDrives=True
        ).execute()
        files = resp.get('files', [])
        if name_suffix:
            files = [f for f in files if f['name'].lower().endswith(name_suffix)]
        results.extend(files)
        page_token = resp.get('nextPageToken')
        if not page_token:
            break
    return results


def get_existing_mp3_names(service, voicemp3_folder_id):
    files = list_files_in_folder(service, voicemp3_folder_id, name_suffix='.mp3')
    return {os.path.splitext(f['name'])[0] for f in files}


def get_start_page_token(service):
    resp = service.changes().getStartPageToken(
        supportsAllDrives=True,
        includeItemsFromAllDrives=True
    ).execute()
    return resp.get('startPageToken')


def register_changes_watch(service, webhook_url, page_token, channel_id=None):
    if not channel_id:
        channel_id = str(uuid.uuid4())
    expiration_ms = int(
        (datetime.datetime.utcnow() + datetime.timedelta(days=7)).timestamp() * 1000
    )
    body = {
        'id': channel_id,
        'type': 'web_hook',
        'address': webhook_url,
        'expiration': expiration_ms
    }
    response = service.changes().watch(
        pageToken=page_token,
        body=body,
        supportsAllDrives=True,
        includeItemsFromAllDrives=True
    ).execute()
    logger.info(f'Changes watch registered: {response}')
    return response


def process_new_files_in_background(call_folder_id, voicemp3_folder_id):
    with _processing_lock:
        try:
            service = get_drive_service()
            logger.info('[WATCH] Scanning CallRecordings for new .m4a files...')
            m4a_files = list_files_in_folder(service, call_folder_id, name_suffix='.m4a')
            existing_mp3s = get_existing_mp3_names(service, voicemp3_folder_id)
            logger.info(f'[WATCH] Found {len(m4a_files)} .m4a files, {len(existing_mp3s)} already converted.')
            for f in m4a_files:
                file_id = f['id']
                file_name = f['name']
                base_name = os.path.splitext(file_name)[0]
                if file_id in _processed_ids:
                    logger.info(f'[SKIP-MEM] {file_name} already in memory cache.')
                    continue
                if base_name in existing_mp3s:
                    logger.info(f'[SKIP-DRIVE] {file_name} already converted in Voicemp3.')
                    _processed_ids.add(file_id)
                    continue
                logger.info(f'[CONVERT] Starting conversion: {file_name} (id={file_id})')
                try:
                    with tempfile.TemporaryDirectory() as tmpdir:
                        m4a_path = os.path.join(tmpdir, file_name)
                        mp3_name = base_name + '.mp3'
                        mp3_path = os.path.join(tmpdir, mp3_name)
                        download_file(service, file_id, m4a_path)
                        convert_m4a_to_mp3(m4a_path, mp3_path)
                        result = upload_file(service, mp3_path, mp3_name, voicemp3_folder_id)
                        _processed_ids.add(file_id)
                        existing_mp3s.add(base_name)
                        logger.info(f'[OK] {file_name} -> {result["name"]} (drive_id={result["id"]})')
                except Exception as e:
                    logger.error(f'[FAIL] {file_name}: {e}')
        except Exception as e:
            logger.error(f'[WATCH] Background processing error: {e}')


@app.route('/health', methods=['GET'])
def health():
    return jsonify({'status': 'ok', 'service': 'ffmpeg-cloudrun-converter'}), 200


@app.route('/watch', methods=['POST'])
def watch():
    resource_state = request.headers.get('X-Goog-Resource-State', '')
    changed = request.headers.get('X-Goog-Changed', '')
    logger.info(f'[WATCH] Received notification: state={resource_state}, changed={changed}')

    if resource_state == 'sync':
        return '', 200

    call_folder_id = os.environ.get('CALL_RECORDINGS_FOLDER_ID')
    voicemp3_folder_id = os.environ.get('VOICEMP3_FOLDER_ID')

    if not call_folder_id or not voicemp3_folder_id:
        logger.error('[WATCH] Missing folder env vars')
        return jsonify({'error': 'Missing folder env vars'}), 500

    t = threading.Thread(
        target=process_new_files_in_background,
        args=(call_folder_id, voicemp3_folder_id),
        daemon=True
    )
    t.start()
    return '', 200


@app.route('/get-page-token', methods=['GET'])
def get_page_token_endpoint():
    """Returns current startPageToken for changes API - used by Apps Script to register watch."""
    try:
        service = get_drive_service()
        token = get_start_page_token(service)
        return jsonify({'pageToken': token}), 200
    except Exception as e:
        logger.error(f'[PAGE-TOKEN] Error: {e}')
        return jsonify({'error': str(e)}), 500


@app.route('/register-watch', methods=['POST'])
def register_watch_endpoint():
    data = request.get_json(force=True) or {}
    webhook_url = data.get('webhook_url')
    page_token = data.get('page_token')

    if not webhook_url:
        return jsonify({'error': 'webhook_url required'}), 400

    try:
        service = get_drive_service()
        if not page_token:
            page_token = get_start_page_token(service)
        response = register_changes_watch(service, webhook_url, page_token)
        return jsonify({'status': 'registered', 'channel': response, 'pageToken': page_token}), 200
    except Exception as e:
        logger.error(f'[REGISTER-WATCH] Error: {e}')
        return jsonify({'error': str(e)}), 500


@app.route('/sync', methods=['POST'])
def sync_all():
    call_folder_id = os.environ.get('CALL_RECORDINGS_FOLDER_ID')
    voicemp3_folder_id = os.environ.get('VOICEMP3_FOLDER_ID')
    if not call_folder_id or not voicemp3_folder_id:
        return jsonify({'error': 'Missing CALL_RECORDINGS_FOLDER_ID or VOICEMP3_FOLDER_ID'}), 500
    t = threading.Thread(
        target=process_new_files_in_background,
        args=(call_folder_id, voicemp3_folder_id),
        daemon=True
    )
    t.start()
    return jsonify({'status': 'sync started in background'}), 200


@app.route('/convert', methods=['POST'])
def convert():
    data = request.get_json(force=True)
    if not data:
        return jsonify({'error': 'Request body must be JSON'}), 400
    file_id = data.get('file_id')
    file_name = data.get('file_name', 'output.m4a')
    target_folder_id = data.get('target_folder_id')
    if not file_id or not target_folder_id:
        return jsonify({'error': 'file_id and target_folder_id are required'}), 400
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
            _processed_ids.add(file_id)
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
