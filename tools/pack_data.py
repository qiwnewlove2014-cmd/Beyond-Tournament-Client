import os
import sys
import shutil
from cryptography.fernet import Fernet

def main():
    print("Packing data folder...")
    # Make sure we run from the client root
    if not os.path.exists("data"):
        print("Data folder not found! Please run from the 'client' directory.")
        sys.exit(1)
    
    # 1. Zip the data folder
    print("Zipping data folder...")
    # shutil.make_archive(base_name, format, root_dir)
    # This will zip the *contents* of 'data' into 'data_temp.zip'
    shutil.make_archive("data_temp", "zip", "data")
    
    # 2. Encrypt the zip file
    print("Encrypting data archive...")
    obf_key_part1 = "70446f58577153326d664363665454635543324e64616b36"
    obf_key_part2 = "30626a74476d364e797030536a5433316f51673d"
    key = bytes.fromhex(obf_key_part1 + obf_key_part2)
    f = Fernet(key)
    
    with open("data_temp.zip", "rb") as file:
        file_data = file.read()
        
    encrypted_data = f.encrypt(file_data)
    
    with open("sounds.dat", "wb") as file:
        file.write(encrypted_data)
        
    # 3. Clean up
    os.remove("data_temp.zip")
    print("Data packed and encrypted to sounds.dat successfully.")

if __name__ == "__main__":
    main()
