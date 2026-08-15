import os
import sys
import shutil
import tempfile
import atexit
import io
import json
import zipfile
from cryptography.fernet import Fernet
import libs.consts as consts

PAK_PATH = "sounds.dat"
SERVER_CONFIG_MEMBER = ".bt/server_endpoint.json"
TEMP_DIR = None
VFS_INITIALIZED = False
EMBEDDED_SERVER_CONFIG = None


def get_embedded_server_config():
    """Return a copy of the production endpoint loaded from the VFS package."""

    if EMBEDDED_SERVER_CONFIG is None:
        return None
    return dict(EMBEDDED_SERVER_CONFIG)

def init_vfs():
    global TEMP_DIR, VFS_INITIALIZED, EMBEDDED_SERVER_CONFIG
    
    if VFS_INITIALIZED:
        return
        
    if not os.path.exists(PAK_PATH):
        if os.path.exists("data"):
            print("VFS: Running from source (data folder found), skipping decryption.")
            consts.SOUNDPREPEND = "data/"
            consts.SOUNDSPREPEND = "/data/"
            EMBEDDED_SERVER_CONFIG = None
            VFS_INITIALIZED = True
            return
        else:
            print(f"VFS Error: Missing both {PAK_PATH} and data/ folder!")
            sys.exit(1)
            
    print(f"VFS: Initializing Virtual File System from {PAK_PATH}...")
    # Create secure temporary directory
    TEMP_DIR = tempfile.mkdtemp(prefix=".bt_cache_")
    
    # Register cleanup so it deletes on normal or abnormal exit
    atexit.register(cleanup_vfs)
    
    # Decryption Key (Obfuscated to prevent raw string scanning in compiled .exe)
    # The original key is hex-encoded and split to break the regex pattern.
    obf_key_part1 = "70446f58577153326d664363665454635543324e64616b36"
    obf_key_part2 = "30626a74476d364e797030536a5433316f51673d"
    key = bytes.fromhex(obf_key_part1 + obf_key_part2)
    f = Fernet(key)
    
    try:
        with open(PAK_PATH, "rb") as file:
            encrypted_data = file.read()
            
        decrypted_data = f.decrypt(encrypted_data)
        
        # Extract straight into TEMP_DIR (so TEMP_DIR contains the assets, without a 'data' subfolder)
        # Keep the decrypted archive in memory so the embedded endpoint is never
        # written as a plaintext ZIP in the temporary directory.
        with zipfile.ZipFile(io.BytesIO(decrypted_data), 'r') as zip_ref:
            try:
                raw_server_config = zip_ref.read(SERVER_CONFIG_MEMBER)
            except KeyError:
                EMBEDDED_SERVER_CONFIG = None
            else:
                parsed_server_config = json.loads(raw_server_config.decode("utf-8"))
                if not isinstance(parsed_server_config, dict):
                    raise ValueError("Embedded server configuration must be a JSON object")
                EMBEDDED_SERVER_CONFIG = parsed_server_config

            # We want to extract it so that the contents of 'data' are directly in TEMP_DIR
            # But shutil.make_archive("data_temp", "zip", "data") makes a zip where the *contents* of data are at the root of the zip.
            # Keep the endpoint in memory instead of extracting it as plaintext.
            for member in zip_ref.infolist():
                if member.filename.replace("\\", "/") == SERVER_CONFIG_MEMBER:
                    continue
                zip_ref.extract(member, TEMP_DIR)
            
        # Override paths so the game looks in the temp folder instead of data/
        # Make sure it ends with a slash for compatibility
        consts.SOUNDPREPEND = TEMP_DIR.replace("\\", "/")
        if not consts.SOUNDPREPEND.endswith("/"):
            consts.SOUNDPREPEND += "/"
        consts.SOUNDSPREPEND = "/" + consts.SOUNDPREPEND
        
        print("VFS: Assets securely extracted and mounted.")
        VFS_INITIALIZED = True
        
    except Exception as e:
        print(f"VFS Decryption Error: {e}")
        cleanup_vfs()
        sys.exit(1)

def cleanup_vfs():
    global TEMP_DIR, EMBEDDED_SERVER_CONFIG
    if TEMP_DIR and os.path.exists(TEMP_DIR):
        try:
            shutil.rmtree(TEMP_DIR, ignore_errors=True)
        except:
            pass
    TEMP_DIR = None
    EMBEDDED_SERVER_CONFIG = None
