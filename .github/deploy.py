import os
from huggingface_hub import HfApi

api = HfApi(token=os.environ["HF_TOKEN"])
user = api.whoami()["name"]
repo_id = f"{user}/qq-nvpu"
print("HF user:", user, "| space:", repo_id)
api.create_repo(repo_id=repo_id, repo_type="space", space_sdk="docker", private=False, exist_ok=True)
print("space created")
api.upload_file(path_or_fileobj="bot_hf.py", path_in_repo="bot_hf.py", repo_id=repo_id, repo_type="space")
api.upload_file(path_or_fileobj="Dockerfile", path_in_repo="Dockerfile", repo_id=repo_id, repo_type="space")
print("files uploaded")
for k in ["QQ_APPID", "QQ_SECRET", "INTERN_KEY", "ZHIPU_KEY"]:
    api.add_space_secret(repo_id=repo_id, key=k, value=os.environ[k])
    print("secret set:", k)
try:
    api.request_space_hardware(repo_id=repo_id, hardware="cpu-basic", repo_type="space")
except Exception as e:
    print("hw (may be ok to ignore):", e)
try:
    api.restart_space(repo_id=repo_id, repo_type="space")
except Exception as e:
    print("restart (may be ok to ignore):", e)
print("DEPLOY DONE")
