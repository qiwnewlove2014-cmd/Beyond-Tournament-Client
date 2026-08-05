import os
import sys
import shutil
import tempfile
import atexit
import zipfile
from cryptography.fernet import Fernet
import libs.consts as consts

PAK_PATH = "sound.dat"
TEMP_DIR = None
VFS_INITIALIZED = False

def init_vfs():
    global TEMP_DIR, VFS_INITIALIZED
    
    if VFS_INITIALIZED:
        return
        
    if not os.path.exists(PAK_PATH):
        if os.path.exists("data"):
            print("VFS: Running from source (data folder found), skipping decryption.")
            consts.SOUNDPREPEND = "data/"
            consts.SOUNDSPREPEND = "/data/"
            VFS_INITIALIZED = True
            return
        else:
            print("VFS Error: Missing both data.pak and data/ folder!")
            sys.exit(1)
            
    print("VFS: Initializing Virtual File System from data.pak...")
    # Create secure temporary directory
    TEMP_DIR = tempfile.mkdtemp(prefix=".bt_cache_")
    
    # Register cleanup so it deletes on normal or abnormal exit
    atexit.register(cleanup_vfs)
    
    # Decryption Key (Baked into compiled .exe)
    key = b"pDoXWqS2mfCcfTTcUC2Ndak60bjtGm6Nyp0SjT31oQg="
    f = Fernet(key)
    
    try:
        with open(PAK_PATH, "rb") as file:
            encrypted_data = file.read()
            
        decrypted_data = f.decrypt(encrypted_data)
        
        temp_zip = os.path.join(TEMP_DIR, "data_temp.zip")
        with open(temp_zip, "wb") as file:
            file.write(decrypted_data)
            
        # Extract straight into TEMP_DIR (so TEMP_DIR contains the assets, without a 'data' subfolder)
        with zipfile.ZipFile(temp_zip, 'r') as zip_ref:
            # We want to extract it so that the contents of 'data' are directly in TEMP_DIR
            # But shutil.make_archive("data_temp", "zip", "data") makes a zip where the *contents* of data are at the root of the zip.
            zip_ref.extractall(TEMP_DIR)
            
        os.remove(temp_zip)
        
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
    global TEMP_DIR
    if TEMP_DIR and os.path.exists(TEMP_DIR):
        try:
            shutil.rmtree(TEMP_DIR, ignore_errors=True)
            TEMP_DIR = None
        except:
            pass
