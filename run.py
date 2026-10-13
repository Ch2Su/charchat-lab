"""로컬 실행: python run.py  →  브라우저에서 http://127.0.0.1:8000"""
import uvicorn

from app import config

if __name__ == "__main__":
    print(f"charchat-lab 실행 중: http://{config.HOST}:{config.PORT}  (끄려면 Ctrl+C)")
    uvicorn.run("app.main:create_app", factory=True, host=config.HOST, port=config.PORT)
