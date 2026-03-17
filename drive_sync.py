#!/usr/bin/env python3
"""
drive_sync.py - Phase 2: Batch sync script.
Lists all .m4a files in CallRecordings folder, converts each to MP3
via the Cloud Run service, and uploads to Voicemp3 folder.
Skips files already converted (by matching filename in Voicemp3).

Usage:
    export CLOUDRUN_SERVICE_URL=https://YOUR-SERVICE-URL
    export GOOGLE_SERVICE_ACCOUNT_JSON='{ ...json... }'
    export CALL_RECORDINGS_FOLDER_ID=<folder_id>
    export VOICEMP3_FOLDER_ID=<folder_id>
    python drive_sync.py
"""

import os
import json
import logging
import requests
from google.oauth2 import service_account
from googleapiclient.discovery import build

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s'
)
logger = logging.getLogger(__name__)

SCOPES = ['https://www.googleapis.com/auth/drive.readonly']


def get_drive_service():
    creds_json = os.environ['GOOGLE_SERVICE_ACCOUNT_JSON']
    creds_info = json.loads(creds_json)
    credentials = service_account.Credentials.from_service_account_info(
        creds_info, scopes=['https://www.googleapis.com/auth/drive']
    )
    return build('drive', 'v3', credentials=credentials)


def list_files_in_folder(service, folder_id, mime_type=None, name_suffix=None):
    """List all files in a Drive folder, optionally filtered by mime or suffix."""
    results = []
    page_token = None
    query = f"'{folder_id}' in parents and trashed=false"
    if mime_type:
        query += f" and mimeType='{mime_type}'"
    while True:
        resp = service.files().list(
            q=query,
            fields='nextPageToken, files(id, name)',
            pageToken=page_token,
            pageSize=1000
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
    """Return a set of MP3 filenames already present in Voicemp3."""
    files = list_files_in_folder(service, voicemp3_folder_id, name_suffix='.mp3')
    return {f['name'] for f in files}


def convert_via_cloudrun(service_url, file_id, file_name, target_folder_id):
    """Call the Cloud Run /convert endpoint and return the response JSON."""
    url = service_url.rstrip('/') + '/convert'
    payload = {
        'file_id': file_id,
        'file_name': file_name,
        'target_folder_id': target_folder_id
    }
    resp = requests.post(url, json=payload, timeout=300)
    resp.raise_for_status()
    return resp.json()


def main():
    service_url = os.environ['CLOUDRUN_SERVICE_URL']
    call_recordings_folder_id = os.environ['CALL_RECORDINGS_FOLDER_ID']
    voicemp3_folder_id = os.environ['VOICEMP3_FOLDER_ID']

    service = get_drive_service()

    logger.info('Fetching .m4a files from CallRecordings...')
    m4a_files = list_files_in_folder(
        service, call_recordings_folder_id, name_suffix='.m4a'
    )
    logger.info(f'Found {len(m4a_files)} .m4a files.')

    logger.info('Fetching existing MP3s in Voicemp3...')
    existing_mp3s = get_existing_mp3_names(service, voicemp3_folder_id)
    logger.info(f'Already converted: {len(existing_mp3s)} files.')

    skipped = 0
    converted = 0
    failed = 0

    for f in m4a_files:
        base_name = os.path.splitext(f['name'])[0]
        expected_mp3 = base_name + '.mp3'

        if expected_mp3 in existing_mp3s:
            logger.info(f'[SKIP] {f["name"]} already converted.')
            skipped += 1
            continue

        logger.info(f'[CONVERT] {f["name"]} (id={f["id"]})')
        try:
            result = convert_via_cloudrun(
                service_url, f['id'], f['name'], voicemp3_folder_id
            )
            logger.info(
                f'[OK] {f["name"]} -> {result["mp3_file_name"]} '
                f'(drive_id={result["mp3_file_id"]})'
            )
            existing_mp3s.add(expected_mp3)
            converted += 1
        except Exception as e:
            logger.error(f'[FAIL] {f["name"]}: {e}')
            failed += 1

    logger.info(
        f'Done. Converted={converted}, Skipped={skipped}, Failed={failed}'
    )


if __name__ == '__main__':
    main()
