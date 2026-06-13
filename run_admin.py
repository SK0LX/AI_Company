"""Run the admin panel (v2). Local-only: binds to 127.0.0.1.

    python run_admin.py        # then open http://127.0.0.1:8100
"""
import uvicorn

if __name__ == "__main__":
    uvicorn.run("src.web.app:app", host="127.0.0.1", port=8100, reload=False)
