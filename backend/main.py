from fastapi import FastAPI, Request

app = FastAPI()

@app.get("/")
def home():
    return {"message": "地域ナビ API 起動中"}

@app.post("/callback")
async def callback(request: Request):
    body = await request.body()

    print(body.decode("utf-8"))

    return "OK"
