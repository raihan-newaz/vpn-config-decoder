from flask import Flask, request, jsonify
from flask_cors import CORS
import base64
import json
import warnings
import urllib.request
import uuid
import os

warnings.filterwarnings("ignore")

app = Flask(__name__)
CORS(app)

# Local offline DarkTunnel decryption
try:
    import msgpack
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms
    try:
        from cryptography.hazmat.decrepit.ciphers.modes import CFB
    except ImportError:
        from cryptography.hazmat.primitives.ciphers.modes import CFB
    from cryptography.hazmat.backends import default_backend
    OFFLINE_SUPPORTED = True
except Exception:
    OFFLINE_SUPPORTED = False

DT_KEY_256 = b"$B&E)H@McQfThWmZq4t7w!z%C*F-JaNd"
DT_KEY_192 = b"F)J@NcRfUjXn2r4u7x!A%D*G"
DT_IV = bytes.fromhex("232e39185523184a5723586242200e05")

def _b64_decode_safe(s):
    s = s.replace("-", "+").replace("_", "/")
    pad = len(s) % 4
    if pad != 0:
        s += "=" * (4 - pad)
    return base64.b64decode(s)

def _aes_cfb_decrypt(data, key, iv):
    cipher = Cipher(algorithms.AES(key), CFB(iv), backend=default_backend())
    decryptor = cipher.decryptor()
    return decryptor.update(data) + decryptor.finalize()

def _clean_encrypted_dt(value, key, iv):
    if isinstance(value, dict):
        cleaned = {}
        for k, v in value.items():
            if isinstance(k, str) and k.startswith("Encrypted") and isinstance(v, (bytes, bytearray)) and len(v) > 0:
                try:
                    dec = _aes_cfb_decrypt(v, key, iv)
                    cleaned[k] = dec.decode('utf-8', errors='replace')
                    continue
                except Exception:
                    pass
            cleaned[k] = _clean_encrypted_dt(v, key, iv)
        return cleaned
    elif isinstance(value, list):
        return [_clean_encrypted_dt(x, key, iv) for x in value]
    elif isinstance(value, (bytes, bytearray)):
        try:
            return value.decode('utf-8')
        except Exception:
            return value
    return value

def decrypt_dark_text(raw_text):
    text = raw_text.strip()
    if text.startswith("darktunnel://"):
        text = text[len("darktunnel://"):].strip()

    outer_bytes = _b64_decode_safe(text)
    outer = json.loads(outer_bytes.decode('utf-8', errors='ignore'))
    
    enc_locked_str = outer.get("encryptedLockedConfig")
    if not enc_locked_str:
        return outer
    
    enc_locked_bytes = _b64_decode_safe(enc_locked_str)
    dec_outer = _aes_cfb_decrypt(enc_locked_bytes, DT_KEY_256, DT_IV)
    
    unpacked_outer = msgpack.unpackb(dec_outer, raw=False)
    if "EncryptedLockedConfig" in unpacked_outer:
        enc_inner_val = unpacked_outer["EncryptedLockedConfig"]
        if isinstance(enc_inner_val, (bytes, bytearray)):
            dec_inner = _aes_cfb_decrypt(enc_inner_val, DT_KEY_192, DT_IV)
            unpacked_inner = msgpack.unpackb(dec_inner, raw=False)
            unpacked_outer["EncryptedLockedConfig"] = _clean_encrypted_dt(unpacked_inner, DT_KEY_192, DT_IV)
            
    outer["encryptedLockedConfig"] = unpacked_outer
    return outer

def proxy_upstream(endpoint, data_dict, files_dict=None):
    boundary = f"----WebKitFormBoundary{uuid.uuid4().hex}"
    body = bytearray()
    for k, v in data_dict.items():
        body.extend(f"--{boundary}\r\n".encode("utf-8"))
        body.extend(f'Content-Disposition: form-data; name="{k}"\r\n\r\n'.encode("utf-8"))
        body.extend(f"{v}\r\n".encode("utf-8"))
    if files_dict:
        for k, (fname, fbytes, ctype) in files_dict.items():
            body.extend(f"--{boundary}\r\n".encode("utf-8"))
            body.extend(f'Content-Disposition: form-data; name="{k}"; filename="{fname}"\r\n'.encode("utf-8"))
            body.extend(f"Content-Type: {ctype}\r\n\r\n".encode("utf-8"))
            body.extend(fbytes)
            body.extend(b"\r\n")
    body.extend(f"--{boundary}--\r\n".encode("utf-8"))
    
    req = urllib.request.Request(
        f"https://config-decoder-two.vercel.app{endpoint}",
        data=body,
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
        }
    )
    with urllib.request.urlopen(req, timeout=30) as res:
        return json.loads(res.read().decode("utf-8"))

@app.route('/api/decode', methods=['POST'])
def handle_decode():
    decoder = request.form.get('decoder', 'DARKTUNNEL')
    input_type = request.form.get('input_type', 'file')
    
    # Try local decode if DARKTUNNEL
    if decoder == 'DARKTUNNEL' and OFFLINE_SUPPORTED:
        try:
            if input_type == 'file' and 'file' in request.files:
                uploaded = request.files['file']
                text_content = uploaded.read().decode('utf-8', errors='ignore')
                raw_filename = uploaded.filename or "darktunnel.dark"
            else:
                text_content = request.form.get('text', '')
                raw_filename = "darktunnel_pasted.dark"
            
            if text_content.strip():
                decrypted_json = decrypt_dark_text(text_content)
                result_str = json.dumps(decrypted_json, indent=4, ensure_ascii=False)
                return jsonify({
                    "filename": raw_filename.replace('.dark', '') + "_decoded.json",
                    "format": "json",
                    "mode": "decode",
                    "result": result_str,
                    "engine": "local-native"
                })
        except Exception as err:
            pass # fallback to upstream

    # Fallback to upstream proxy for all formats (HTTPCUSTOM, HTTPINJECTOR, NPVTUNNEL, SSCCUSTOM, etc.)
    data_dict = {"decoder": decoder, "input_type": input_type}
    files_dict = {}
    if input_type == 'file' and 'file' in request.files:
        f = request.files['file']
        f.seek(0)
        files_dict['file'] = (f.filename, f.read(), f.content_type or 'application/octet-stream')
    else:
        data_dict['text'] = request.form.get('text', '')
        
    try:
        upstream_resp = proxy_upstream('/decode', data_dict, files_dict)
        return jsonify(upstream_resp)
    except Exception as e:
        return jsonify({"error": f"Decoding failed: {str(e)}"}), 500

@app.route('/api/encode', methods=['POST'])
def handle_encode():
    decoder = request.form.get('decoder', 'DARKTUNNEL')
    input_type = request.form.get('input_type', 'text')
    data_dict = {"decoder": decoder, "input_type": input_type}
    files_dict = {}
    if input_type == 'file' and 'file' in request.files:
        f = request.files['file']
        files_dict['file'] = (f.filename, f.read(), f.content_type or 'application/octet-stream')
    else:
        data_dict['text'] = request.form.get('text', '')

    try:
        upstream_resp = proxy_upstream('/encode', data_dict, files_dict)
        return jsonify(upstream_resp)
    except Exception as e:
        return jsonify({"error": f"Encoding failed: {str(e)}"}), 500

@app.route('/api/health', methods=['GET'])
def health():
    return jsonify({"status": "ok", "offline_supported": OFFLINE_SUPPORTED})

if __name__ == '__main__':
    app.run(port=3000, debug=True)
