"""Trusted reference functional probe. Does not launch repository code.
The real controller must supply the run's approved private network namespace and enforce
case/attempt binding. An arbitrary client's JSON is not trusted proof evidence.
"""
from __future__ import annotations
import argparse
import http.client
import json
import uuid
from typing import Any

class ProbeFailure(RuntimeError):
    pass

def request(port: int, method: str, path: str, payload: dict[str, Any] | None = None) -> tuple[int, dict[str, Any]]:
    connection = http.client.HTTPConnection('127.0.0.1', port, timeout=5)
    try:
        raw = None if payload is None else json.dumps(payload).encode()
        connection.request(method, path, body=raw, headers={'Content-Type': 'application/json'})
        response = connection.getresponse()
        data = response.read(262145)
        if len(data) > 262144:
            raise ProbeFailure('Oversized response')
        # http.client does not follow redirects to an untrusted destination.
        parsed = json.loads(data)
        if not isinstance(parsed, dict):
            raise ProbeFailure('Expected JSON object')
        return response.status, parsed
    finally:
        connection.close()

def probe(port: int) -> dict[str, Any]:
    if type(port) is not int or not 1024 <= port <= 65535:
        raise ProbeFailure('Invalid controller-assigned port')
    nonce = 'firstrun-' + uuid.uuid4().hex
    status, created = request(port, 'POST', '/notes', {'message': nonce})
    if status != 201 or type(created.get('id')) is not int or created['id'] <= 0:
        raise ProbeFailure(f'Create failed: HTTP {status}: {created}')
    status, fetched = request(port, 'GET', f"/notes/{created['id']}")
    if status != 200 or fetched.get('id') != created['id'] or fetched.get('message') != nonce:
        raise ProbeFailure(f'Read-back mismatch: HTTP {status}')
    return {'verifier_id': 'notes-create-read-v1', 'outcome': 'passed', 'checks': ['create_note', 'read_back_same_note']}

def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--port', required=True, type=int)
    args = p.parse_args()
    try:
        print(json.dumps(probe(args.port)))
        return 0
    except (ProbeFailure, OSError, ValueError, http.client.HTTPException) as exc:
        print(json.dumps({'verifier_id': 'notes-create-read-v1', 'outcome': 'failed', 'error': str(exc)}))
        return 1
if __name__ == '__main__':
    raise SystemExit(main())
