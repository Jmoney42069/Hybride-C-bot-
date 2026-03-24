import subprocess
import shutil
import os
import zipfile

print("Building NVL Compliance (onedir mode)...")

PYTHON = r"C:\Users\User\AppData\Local\Programs\Python\Python313\python.exe"

# Clean vorige build
for d in ["build", "dist"]:
    if os.path.isdir(d):
        shutil.rmtree(d)
        print(f"  Cleaned {d}/")

# Build via .spec file (onedir mode)
subprocess.run([
    PYTHON, "-m", "PyInstaller",
    "--clean",
    "NVL-Compliance.spec",
], check=True)

# Maak zip van de output folder
dist_folder = os.path.join("dist", "NVL-Compliance")
out_dir = r"C:\Users\User\Desktop\NVL-Compliance-Agent"

if os.path.isdir(out_dir):
    shutil.rmtree(out_dir)

# Kopieer hele folder
shutil.copytree(dist_folder, out_dir)

# Maak ook een zip
zip_path = r"C:\Users\User\Desktop\NVL-Compliance-Agent.zip"
if os.path.exists(zip_path):
    os.remove(zip_path)

print("Creating zip...")
with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
    for root, dirs, files in os.walk(dist_folder):
        for file in files:
            full = os.path.join(root, file)
            arc = os.path.join("NVL-Compliance", os.path.relpath(full, dist_folder))
            zf.write(full, arc)

zip_size = os.path.getsize(zip_path) / (1024 * 1024)
n_files = sum(len(f) for _, _, f in os.walk(out_dir))
print(f"Build klaar!")
print(f"  Folder: {out_dir} ({n_files} bestanden)")
print(f"  Zip:    {zip_path} ({zip_size:.1f} MB)")
