"""Alibaba Function Compute entry point for weekly Notion time reports."""
from __future__ import annotations

import json, logging, math, os, smtplib, struct, zlib
from collections import defaultdict
from datetime import date, datetime, time, timedelta
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from zoneinfo import ZoneInfo

from notion_calendar_export import DEFAULT_PARENT_PAGE_ID, NotionClient, clean_id, discover_databases, prop_text

LOG = logging.getLogger()
LOG.setLevel(logging.INFO)
PALETTE = ["#4f9b55", "#d30b0b", "#ca68a2", "#087ffa", "#0788b8", "#d0bf2d", "#8b8b8b", "#ff8700", "#c100df", "#b77b50", "#91b5b8"]
FONT = {
    "0": ("01110","10001","10011","10101","11001","10001","01110"),
    "1": ("00100","01100","00100","00100","00100","00100","01110"),
    "2": ("01110","10001","00001","00010","00100","01000","11111"),
    "3": ("11110","00001","00001","01110","00001","00001","11110"),
    "4": ("00010","00110","01010","10010","11111","00010","00010"),
    "5": ("11111","10000","10000","11110","00001","00001","11110"),
    "6": ("01110","10000","10000","11110","10001","10001","01110"),
    "7": ("11111","00001","00010","00100","01000","01000","01000"),
    "8": ("01110","10001","10001","01110","10001","10001","01110"),
    "9": ("01110","10001","10001","01111","00001","00001","01110"),
    "/": ("00001","00010","00010","00100","01000","01000","10000"),
    ".": ("00000","00000","00000","00000","00000","00110","00110"),
}

class Raster:
    def __init__(self, width, height):
        self.width, self.height = width, height
        self.data = bytearray(bytes((255,255,255)) * (width * height))
    def pixel(self, x, y, color):
        if 0 <= x < self.width and 0 <= y < self.height:
            i = (y*self.width+x)*3
            self.data[i:i+3] = bytes(color)
    def rect(self, x0, y0, x1, y1, color):
        x0,y0,x1,y1 = max(0,int(x0)),max(0,int(y0)),min(self.width,int(x1)),min(self.height,int(y1))
        row = bytes(color) * max(0,x1-x0)
        for y in range(y0,y1):
            i = (y*self.width+x0)*3
            self.data[i:i+len(row)] = row
    def line(self, x0, y0, x1, y1, color):
        dx,sx = abs(x1-x0), 1 if x0<x1 else -1
        dy,sy = -abs(y1-y0), 1 if y0<y1 else -1
        err = dx+dy
        while True:
            self.pixel(x0,y0,color)
            if x0==x1 and y0==y1: break
            e=2*err
            if e>=dy: err+=dy; x0+=sx
            if e<=dx: err+=dx; y0+=sy
    def text(self, x, y, value, color, scale=2):
        cursor=int(x)
        for char in value:
            glyph=FONT.get(char)
            if glyph:
                for row,bits in enumerate(glyph):
                    for col,bit in enumerate(bits):
                        if bit=="1": self.rect(cursor+col*scale,y+row*scale,cursor+(col+1)*scale,y+(row+1)*scale,color)
            cursor+=6*scale
    def png(self):
        raw=b"".join(b"\x00"+self.data[y*self.width*3:(y+1)*self.width*3] for y in range(self.height))
        def chunk(kind,data):
            body=kind+data
            return struct.pack(">I",len(data))+body+struct.pack(">I",zlib.crc32(body)&0xffffffff)
        header=struct.pack(">IIBBBBB",self.width,self.height,8,2,0,0,0)
        return b"\x89PNG\r\n\x1a\n"+chunk(b"IHDR",header)+chunk(b"IDAT",zlib.compress(raw,6))+chunk(b"IEND",b"")

def rgb(value):
    value=value.lstrip("#")
    return tuple(int(value[i:i+2],16) for i in (0,2,4)) if len(value)==6 else (80,120,180)

def color_map(categories):
    custom={}
    try:
        parsed=json.loads(os.getenv("CATEGORY_COLORS_JSON","{}"))
        if isinstance(parsed,dict): custom={str(k):v for k,v in parsed.items() if isinstance(v,str) and len(v)==7 and v.startswith("#")}
    except (ValueError,TypeError): LOG.warning("Invalid CATEGORY_COLORS_JSON ignored")
    result={name:custom.get(name,PALETTE[i%len(PALETTE)]) for i,name in enumerate(categories)}
    result["未记录时长"]=custom.get("未记录时长","#a7bec0")
    return result

def parse_dt(value, zone):
    if not value: return None
    try: dt=datetime.fromisoformat(value.replace("Z","+00:00"))
    except (TypeError,ValueError): return None
    return dt.replace(tzinfo=zone) if dt.tzinfo is None else dt.astimezone(zone)

def report_week(now, requested=""):
    if requested:
        start=date.fromisoformat(requested)
        if start.weekday()!=0: raise ValueError("week_start must be a Monday (YYYY-MM-DD)")
        return start
    monday=now.date()-timedelta(days=now.weekday())
    return monday-timedelta(days=7)

def fetch_records(zone):
    token=os.getenv("NOTION_API_KEY","").strip()
    if not token: raise RuntimeError("Missing NOTION_API_KEY")
    parent=clean_id(os.getenv("NOTION_PARENT_PAGE_ID",DEFAULT_PARENT_PAGE_ID))
    client=NotionClient(token)
    databases=discover_databases(client,parent)
    if not databases: raise RuntimeError("No child databases found under Parent databases")
    records=[]; seen=set(); categories=[db["title"] for db in databases]
    for db in databases:
        info=client.request("GET",f"/databases/{db['id']}")
        for source in info.get("data_sources",[]):
            for page in client.data_source_rows(source["id"]):
                pid=page.get("id","")
                if pid and pid in seen: continue
                if pid: seen.add(pid)
                props=page.get("properties",{})
                dp=props.get("Event Date",props.get("Date",props.get("日期",{})))
                date_value=dp.get("date") if dp.get("type")=="date" else None
                start=parse_dt((date_value or {}).get("start"),zone)
                end=parse_dt((date_value or {}).get("end"),zone)
                duration_value=prop_text(props.get("Time allotted",props.get("Duration",props.get("时长",{})))).strip()
                try: duration=float(duration_value) if duration_value else None
                except ValueError: duration=None
                if duration is not None and not math.isfinite(duration): duration=None
                elapsed=(end-start).total_seconds()/3600 if start and end and end>=start else None
                if duration is None: duration=elapsed
                title=props.get("Event",next((p for p in props.values() if p.get("type")=="title"),{}))
                records.append({"id":pid,"category":db["title"],"event":prop_text(title),"start":start,"end":end,"duration":duration,"elapsed":elapsed})
    return records,categories

def merge_unlogged(records, week_start, zone):
    result={}
    for i in range(7):
        day=week_start+timedelta(days=i)
        low=datetime.combine(day,time.min,zone); high=low+timedelta(days=1)
        spans=sorted((max(r["start"],low),min(r["end"],high)) for r in records if r["start"] and r["end"] and r["end"]>r["start"] and r["start"]<high and r["end"]>low)
        covered=0.0; a=b=None
        for x,y in spans:
            if a is None: a,b=x,y
            elif x<=b: b=max(b,y)
            else: covered+=(b-a).total_seconds()/3600; a,b=x,y
        if a is not None: covered+=(b-a).total_seconds()/3600
        result[day]=max(0.0,24.0-covered)
    return result

def make_report(records,categories,week_start,zone):
    week_end=week_start+timedelta(days=7)
    selected=[r for r in records if r["start"] is None or week_start<=r["start"].date()<week_end]
    totals={name:0.0 for name in categories}
    daily={week_start+timedelta(days=i):defaultdict(float) for i in range(7)}
    missing=undated=0
    for r in selected:
        if r["duration"] is None: missing+=1; continue
        totals[r["category"]]=totals.get(r["category"],0)+r["duration"]
        if r["start"] is None: undated+=1
        else: daily[r["start"].date()][r["category"]]+=r["duration"]
    unlogged=merge_unlogged(selected,week_start,zone)
    for day,hours in unlogged.items(): daily[day]["未记录时长"]+=hours
    totals["未记录时长"]=sum(unlogged.values())
    timed=sorted((r for r in selected if r["start"] and r["end"] and r["end"]>r["start"]),key=lambda r:r["start"])
    active=[]; overlap=set()
    for r in timed:
        active=[x for x in active if x["end"]>r["start"]]
        for x in active: overlap.update((x["id"],r["id"]))
        active.append(r)
    discrepancy=sum(1 for r in selected if r["duration"] is not None and r["elapsed"] is not None and abs(r["duration"]-r["elapsed"])>1/60)
    return {"start":week_start,"end":week_end-timedelta(days=1),"categories":categories,"totals":totals,"daily":daily,
            "total":sum(totals.values()),"missing":missing,"undated":undated,"overlap":len(overlap),"discrepancy":discrepancy,"records":len(selected)}

def donut_png(report,colors):
    image=Raster(760,500); center=(380,240); outer,inner=190,105
    rows=sorted(report["totals"].items(),key=lambda x:(-x[1],x[0]))
    total=sum(max(v,0) for _,v in rows)
    if total:
        bounds=[]; cursor=0
        for name,value in rows:
            amount=max(0,value); bounds.append((cursor,cursor+amount/total*2*math.pi,rgb(colors.get(name,"#8190a5")))); cursor+=amount/total*2*math.pi
        cx,cy=center
        for y in range(cy-outer,cy+outer):
            for x in range(cx-outer,cx+outer):
                dx,dy=x-cx,y-cy; radius=math.hypot(dx,dy)
                if inner<=radius<=outer:
                    angle=(math.atan2(dy,dx)+math.pi/2)%(2*math.pi)
                    for a,b,color in bounds:
                        if a<=angle<b: image.pixel(x,y,color); break
    image.text(center[0]-34,center[1]-10,f"{report['total']:.1f}",(35,48,65),scale=4)
    return image.png()

def trend_png(report,colors):
    cats=sorted(report["categories"],key=lambda n:(-report["totals"].get(n,0),n))
    cats=[n for n in cats if any(report["daily"][d].get(n,0)>0 for d in report["daily"])]
    if any(report["daily"][d].get("未记录时长",0)>0 for d in report["daily"]): cats.append("未记录时长")
    days=list(report["daily"]); totals=[sum(max(0,report["daily"][d].get(n,0)) for n in cats) for d in days]
    maxh=max(4,math.ceil(max(totals,default=0)/4)*4)
    image=Raster(1100,700); left,right,top,bottom=100,24,36,115; pw=image.width-left-right; ph=image.height-top-bottom
    for hour in range(maxh+1):
        y=top+ph-round(hour/maxh*ph); image.line(left,y,image.width-right,y,(220,228,238) if hour%4==0 else (240,243,247))
        image.text(left-46,y-7,str(hour),(79,96,116),scale=2)
    slot=pw/7; bw=min(65,slot*.6)
    for i,day in enumerate(days):
        xc=left+slot*(i+.5); base=top+ph
        for cat in cats:
            value=max(0,report["daily"][day].get(cat,0))
            if value:
                y=base-value/maxh*ph; image.rect(xc-bw/2,y,xc+bw/2,base,rgb(colors.get(cat,"#8190a5"))); base=y
        label=day.strftime("%m/%d"); image.text(xc-len(label)*6,top+ph+18,label,(79,96,116),scale=2)
    image.line(left,top,left,top+ph,(176,188,202)); image.line(left,top+ph,image.width-right,top+ph,(176,188,202))
    return image.png()

def html_escape(value):
    return str(value).replace("&","&amp;").replace("<","&lt;").replace(">","&gt;").replace('"',"&quot;").replace("'","&#39;")

def build_email(report,colors):
    denominator=sum(max(v,0) for v in report["totals"].values())
    rows=sorted(report["totals"].items(),key=lambda x:(-x[1],x[0]))
    table="".join("<tr>"+f"<td style='padding:6px 8px'><i style='display:inline-block;width:10px;height:10px;background:{html_escape(colors.get(n,'#8190a5'))};margin-right:8px'></i>{html_escape(n)}</td>"+f"<td style='text-align:right;padding:6px 8px'>{v:.2f} 小时</td><td style='text-align:right;padding:6px 8px'>{(max(v,0)/denominator*100 if denominator else 0):.1f}%</td></tr>" for n,v in rows)
    legend="　".join(f"<span style='white-space:nowrap'><i style='display:inline-block;width:10px;height:10px;background:{html_escape(colors[n])};margin-right:5px'></i>{html_escape(n)}</span>" for n in colors if n in report["categories"] or n=="未记录时长")
    date_range=f"{report['start']:%Y/%m/%d} – {report['end']:%Y/%m/%d}"
    notes=[]
    if report["overlap"]: notes.append(f"{report['overlap']} 条记录涉及时间重叠；保留原始时长，未自动扣除。")
    if report["missing"]: notes.append(f"{report['missing']} 条记录缺少有效时长，未计入小时汇总。")
    if report["undated"]: notes.append(f"{report['undated']} 条有时长记录缺少日期，计入类别汇总但不进入日趋势。")
    if report["discrepancy"]: notes.append(f"{report['discrepancy']} 条记录的时长与起止时间相差超过1分钟。")
    notes_html="".join(f"<li>{html_escape(n)}</li>" for n in notes)
    html=f"""<!doctype html><html><body style="margin:0;background:#f5f6f8;color:#17202b;font:15px/1.55 Arial,'Microsoft YaHei',sans-serif"><div style="max-width:860px;margin:0 auto;padding:24px">
<h1 style="font-size:23px;margin:0 0 5px">时间使用周报</h1><p style="color:#657384;margin:0 0 18px">统计范围：{date_range} · 总计 {report['total']:.2f} 小时</p>
<section style="background:#fff;border:1px solid #e7ebef;border-radius:14px;padding:18px;margin-bottom:16px"><h2 style="font-size:17px">类别时间占比</h2>
<img src="cid:share-chart" alt="类别时间占比" style="width:100%;max-width:760px;height:auto"><table role="presentation" style="width:100%;border-collapse:collapse"><thead><tr><th style="text-align:left">类别</th><th style="text-align:right">时长</th><th style="text-align:right">占比</th></tr></thead><tbody>{table}</tbody></table></section>
<section style="background:#fff;border:1px solid #e7ebef;border-radius:14px;padding:18px;margin-bottom:16px"><h2 style="font-size:17px">时间变化趋势</h2><p style="color:#657384">按日堆叠显示各类别时长，单位：小时。</p><img src="cid:trend-chart" alt="时间变化趋势" style="width:100%;height:auto"><div style="font-size:12px;line-height:2">{legend}</div></section>
{f'<section style="background:#fff7e9;padding:12px 16px"><strong>数据提示</strong><ul>{notes_html}</ul></section>' if notes else ''}</div></body></html>"""
    msg=EmailMessage(); msg["Subject"]=f"时间使用周报｜{report['start']} 至 {report['end']}"
    msg["From"]=os.getenv("MAIL_FROM",os.getenv("MAIL_SMTP_USER","")); msg["To"]=os.environ["REPORT_TO"]
    msg["Date"]=formatdate(localtime=True); msg["Message-ID"]=make_msgid()
    msg.set_content(f"时间使用周报 {date_range}，总计 {report['total']:.2f} 小时。请用支持 HTML 的邮件客户端查看图表。")
    msg.add_alternative(html,subtype="html"); part=msg.get_payload()[-1]
    part.add_related(donut_png(report,colors),maintype="image",subtype="png",cid="<share-chart>",filename="category-share.png")
    part.add_related(trend_png(report,colors),maintype="image",subtype="png",cid="<trend-chart>",filename="time-trend.png")
    return msg

def send(msg):
    host=os.getenv("MAIL_SMTP_HOST","smtpdm.aliyun.com"); port=int(os.getenv("MAIL_SMTP_PORT","465"))
    user=os.getenv("MAIL_SMTP_USER",""); password=os.getenv("MAIL_SMTP_PASSWORD","")
    if not user or not password or not msg.get("From"): raise RuntimeError("Missing MAIL_SMTP_USER, MAIL_SMTP_PASSWORD, or MAIL_FROM")
    if port==465:
        with smtplib.SMTP_SSL(host,port,timeout=30) as server: server.login(user,password); server.send_message(msg)
    else:
        with smtplib.SMTP(host,port,timeout=30) as server:
            server.ehlo(); server.starttls(); server.ehlo(); server.login(user,password); server.send_message(msg)

def event_dict(event):
    if isinstance(event,bytes): event=event.decode("utf-8")
    if isinstance(event,str):
        try:
            value=json.loads(event)
            return value if isinstance(value,dict) else {"payload":value}
        except json.JSONDecodeError: return {"payload":event}
    return event if isinstance(event,dict) else {}

def handler(event,context):
    values=event_dict(event); zone=ZoneInfo(os.getenv("REPORT_TIMEZONE","Asia/Shanghai"))
    week=report_week(datetime.now(zone),str(values.get("week_start","")))
    records,categories=fetch_records(zone); report=make_report(records,categories,week,zone); colors=color_map(categories)
    if values.get("action")=="preview" or values.get("dry_run") is True:
        LOG.info("Preview ready for week %s: %d source records loaded, %d records in report window",week,len(records),report["records"])
        return {"status":"preview_ready","week_start":str(report["start"]),"week_end":str(report["end"]),
                "source_records":len(records),"week_records":report["records"],"records":report["records"],
                "categories":len(categories),"total_hours":round(report["total"],2),"overlap_records":report["overlap"],"missing_duration":report["missing"]}
    message=build_email(report,colors); send(message)
    LOG.info("Weekly report sent for week starting %s",week)
    return {"status":"sent","week_start":str(report["start"]),"week_end":str(report["end"]),"recipient":message["To"],
            "records":report["records"],"total_hours":round(report["total"],2)}
