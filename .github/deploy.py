import os, time, requests
from huggingface_hub import HfApi

api = HfApi(token=os.environ["HF_TOKEN"])
user = api.whoami()["name"]
repo_id = f"{user}/qq-nvpu-bot"     # NEW space, docker sdk
print("HF user:", user, "| target:", repo_id)
api.create_repo(repo_id=repo_id, repo_type="space", space_sdk="docker", private=False, exist_ok=True)
print("space created (docker)")
api.upload_file(path_or_fileobj="bot_hf.py", path_in_repo="bot_hf.py", repo_id=repo_id, repo_type="space")
api.upload_file(path_or_fileobj="Dockerfile", path_in_repo="Dockerfile", repo_id=repo_id, repo_type="space")
print("files uploaded")
for k in ["QQ_APPID", "QQ_SECRET", "INTERN_KEY", "ZHIPU_KEY"]:
    api.add_space_secret(repo_id=repo_id, key=k, value=os.environ[k])
    print("secret set:", k)
try:
    api.restart_space(repo_id=repo_id)
    print("restart requested")
except Exception as e:
    print("restart err:", e)
hdr = {"Authorization": "Bearer " + os.environ["HF_TOKEN"]}
stage = None
for i in range(20):
    try:
        d = requests.get(f"https://huggingface.co/api/spaces/{repo_id}", headers=hdr, timeout=20).json()
        stage = (d.get("runtime") or {}).get("stage")
        print("stage:", stage)
    except Exception as e:
        print("poll err:", e)
    if stage == "RUNNING":
        break
    time.sleep(12)
print("FINAL:", stage)
try:
    logs = requests.get(f"https://huggingface.co/api/spaces/{repo_id}/logs/run", headers=hdr, timeout=20).text
    print("--- run logs tail ---")
    print(logs[-1200:])
except Exception as e:
    print("logs err:", e)
