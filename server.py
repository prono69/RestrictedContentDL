import uvicorn
from fastapi import FastAPI

app = FastAPI(docs_url=None, redoc_url="/")

@app.get("/status")
def hello():
    return {"message": "running"}
    
if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=7860)
    
    