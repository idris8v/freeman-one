"""Authorized photo responses. Previews live in bounded RAM, never duplicate files."""
from collections import OrderedDict
from io import BytesIO
from pathlib import Path
import time
from anyio import CapacityLimiter,to_thread
from fastapi import HTTPException
from fastapi.responses import FileResponse,Response
from PIL import Image,ImageOps

_LIMITER=CapacityLimiter(1)
_HEADERS={'Cache-Control':'private, no-store','X-Content-Type-Options':'nosniff'}
_FORMATS=('JPEG','PNG','WEBP','GIF')
class PreviewCache:
    def __init__(self,max_bytes=8*1024*1024,max_entries=48):
        self.max_bytes,self.max_entries=max_bytes,max_entries
        self.items=OrderedDict();self.bytes=0
    def get(self,key):
        entry=self.items.get(key)
        if not entry:return None
        at,value=entry
        if time.monotonic()-at>180:
            self.items.pop(key);self.bytes-=len(value);return None
        self.items.move_to_end(key);return value
    def put(self,key,value):
        old=self.items.pop(key,None)
        if old:self.bytes-=len(old[1])
        if len(value)>self.max_bytes:return
        self.items[key]=(time.monotonic(),value);self.bytes+=len(value)
        while len(self.items)>self.max_entries or self.bytes>self.max_bytes:
            _,(_,old)=self.items.popitem(last=False);self.bytes-=len(old)
_CACHE=PreviewCache()

def _encode(raw,variant):
    if len(raw)>20*1024*1024:raise ValueError('image_too_large')
    with Image.open(BytesIO(raw),formats=_FORMATS) as source:
        if source.width*source.height>(64_000_000 if source.format=='JPEG' else 16_000_000):raise ValueError('image_dimensions')
        source.seek(0)
        side=28 if variant=='tiny' else 800
        source.draft('RGB',(side,side))
        if source.width*source.height>16_000_000:raise ValueError('image_dimensions')
        image=ImageOps.exif_transpose(source)
        image.thumbnail((side,side),Image.Resampling.LANCZOS)
        image=image.convert('RGBA' if 'A' in image.getbands() or 'transparency' in image.info else 'RGB')
        out=BytesIO();image.save(out,'WEBP',quality=45 if variant=='tiny' else 86,method=2)
        return out.getvalue()

def _variant(preview):
    if preview not in ('1','true','tiny'):raise HTTPException(422,'Неизвестный размер фото')
    return 'tiny' if preview=='tiny' else 'preview'

def _pair(raw):
    return {'tiny':_encode(raw,'tiny'),'preview':_encode(raw,'1')}

async def _prepare(raw,key):
    values=await to_thread.run_sync(_pair,raw)
    for variant,value in values.items():_CACHE.put((key,variant),value)
    return values

async def preview_bytes(raw,cache_key,variant='1'):
    selected=_variant(variant)
    cached=_CACHE.get((cache_key,selected))
    if cached is not None:return cached
    try:
        async with _LIMITER:
            cached=_CACHE.get((cache_key,selected))
            if cached is None:cached=(await _prepare(raw,cache_key))[selected]
            return cached
    except (OSError,ValueError,Image.DecompressionBombError):
        raise HTTPException(422,'Не удалось подготовить превью. Можно скачать оригинал.')

async def photo_response(path:Path,*,kind,mime_type,filename,preview=''):
    if preview and kind=='image':
        selected=_variant(preview);st=path.stat()
        # All callers authorize the resource before using cached bytes.
        key=(str(path),st.st_ino,st.st_mtime_ns,st.st_size)
        cached=_CACHE.get((key,selected))
        if cached is None:
            if st.st_size>20*1024*1024:raise HTTPException(413,'Фото больше 20 МБ')
            try:
                async with _LIMITER:
                    cached=_CACHE.get((key,selected))
                    if cached is None:
                        raw=await to_thread.run_sync(path.read_bytes)
                        cached=(await _prepare(raw,key))[selected]
            except (OSError,ValueError,Image.DecompressionBombError):
                raise HTTPException(422,'Не удалось подготовить превью. Можно скачать оригинал.')
        return Response(cached,media_type='image/webp',headers=_HEADERS)
    return FileResponse(path,media_type=mime_type,filename=filename,headers=_HEADERS)

def compact_photo(raw,mime,suffix):
    """Lossless only; use exactly one stored file and never enlarge uploads."""
    if mime not in ('image/png','image/jpeg','image/webp') or len(raw)<128*1024:return raw,mime,suffix
    try:
        with Image.open(BytesIO(raw),formats=_FORMATS) as source:
            if source.width*source.height>16_000_000 or getattr(source,'is_animated',False):return raw,mime,suffix
            image=ImageOps.exif_transpose(source)
            if image.mode not in ('RGB','RGBA'):image=image.convert('RGBA' if 'transparency' in image.info else 'RGB')
            out=BytesIO();image.save(out,'WEBP',lossless=True,method=1,icc_profile=source.info.get('icc_profile'))
            if out.tell()<len(raw)*.9:return out.getvalue(),'image/webp','.webp'
    except (OSError,ValueError,Image.DecompressionBombError):pass
    return raw,mime,suffix

async def compact_upload(raw,mime,suffix):
    return await to_thread.run_sync(compact_photo,raw,mime,suffix,limiter=_LIMITER)
