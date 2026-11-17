from fastapi import APIRouter,HTTPException
from fastapi.responses import HTMLResponse
from html import escape
from app.web.posts import load_json
from app.database.repositories import SQLRepository

def router(database):
 r=APIRouter()
 @r.get('/broadcast-post/{token}',response_class=HTMLResponse)
 async def post(token:str):
  if len(token)>64:raise HTTPException(404)
  async with database.sessions() as s:value=await load_json(SQLRepository(s),'campaign_post:'+token)
  if not value:raise HTTPException(404,'Пост не найден')
  title=escape(value['title']);body=escape(value['text'])
  return HTMLResponse('<!doctype html><html lang="ru" data-ui-bundle="account"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"><link rel="icon" type="image/png" sizes="64x64" href="/assets/favicon-rounded-20261029.png"><title>'+title+'</title><script src="/assets/theme-init.js?v=0914e88d7a32"></script><link rel="stylesheet" href="/assets/account-management.css?v=5f7d3d8071a8"></head><body><main class="shell"><article class="fo-card fo-scope fo-reading"><p class="fo-reading-meta">Freeman ONE</p><h1>'+title+'</h1><section class="fo-reading-content">'+body+'</section></article></main></body></html>',headers={'Content-Security-Policy':"default-src 'none'; script-src 'self'; style-src 'self'; frame-ancestors 'self' https://web.telegram.org",'Cache-Control':'no-store'})
 return r
