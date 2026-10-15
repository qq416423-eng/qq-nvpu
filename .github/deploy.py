import os, time, requests
from huggingface_hub import HfApi

api = HfApi(token=os.environ["HF_TOKEN"])
user = api.whoami()["name"]
repo_id = f"{user}/qq-nvpu"
print("HF user:", user)
try:
    api.restart_space(repo_id=repo_id)
    print("space restart requested")
except Exception as e:
    print("restart err:", e)
stage = None
for i in range(18):
    try:
        d = requests.get(f"https://huggingface.co/api/spaces/{repo_id}", timeout=15).json()
        stage = (d.get("runtime") or {}).get("stage")
        print("stage:", stage)
    except Exception as e:
        print("poll err:", e)
    if stage == "RUNNING":
        break
    time.sleep(10)
print("FINAL:", stage)
try:
    logs = requests.get(f"https://huggingface.co/api/spaces/{repo_id}/logs/run", timeout=15).text
    print("--- run logs ---")
    print(logs[-1500:])
except Exception as e:
    print("logs err:", e)
