import re,httpx
from bs4 import BeautifulSoup
def analyze_site(url):
    d={"url":url,"reachable":False,"https":url.startswith("https://"),"mobile_viewport":False,"has_booking":False,"has_cta":False,"has_contact":False,"website_score":0,"problems":[]}
    try:
        r=httpx.get(url,timeout=15,follow_redirects=True,headers={"User-Agent":"GarrickAIOutreach/1.0"}); r.raise_for_status()
        s=BeautifulSoup(r.text[:2000000],"html.parser"); html=r.text.lower(); d["reachable"]=True
        d["mobile_viewport"]=bool(s.find("meta",attrs={"name":re.compile("^viewport$",re.I)}))
        d["has_booking"]=any(x in html for x in ["booking","appointment","book now","schedule"])
        d["has_contact"]="contact" in html; d["has_cta"]=any(x in html for x in ["get started","call now","book now","request a quote"])
        score=20+15*d["https"]+20*d["mobile_viewport"]+20*d["has_booking"]+15*d["has_cta"]+10*d["has_contact"]
        d["website_score"]=min(score,100)
        if not d["mobile_viewport"]:d["problems"].append("No mobile viewport")
        if not d["has_booking"]:d["problems"].append("No obvious booking flow")
        if not d["has_cta"]:d["problems"].append("No strong CTA")
    except Exception as e:d["problems"].append(type(e).__name__)
    return d
