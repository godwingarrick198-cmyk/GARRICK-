import os,json
from fastapi import FastAPI,HTTPException
from pydantic import BaseModel,Field
from database import init_db,SessionLocal
from models import Lead,LeadStatus
from services.osm import search_businesses
from services.analyze import analyze_site
from services.gemini import score
from services.telegram import notify

app=FastAPI(title="Garrick AI Outreach",version="1.0.0")
class SearchRequest(BaseModel):
    niche:str=Field(min_length=2,max_length=80)
    city:str=Field(min_length=2,max_length=120)
    lead_count:int=Field(default=10,ge=1,le=50)

@app.on_event("startup")
def startup(): init_db()

@app.get("/api/health")
def health(): return {"status":"ok","service":"garrick-ai-outreach"}

@app.post("/api/search/businesses")
def search(req:SearchRequest):
    try: rows=search_businesses(req.niche,req.city,req.lead_count)
    except Exception as e: raise HTTPException(502,f"Business search failed: {e}")
    saved=[]
    with SessionLocal() as db:
        for x in rows:
            if db.query(Lead).filter_by(source_id=x["source_id"]).first(): continue
            lead=Lead(**x); db.add(lead); saved.append(lead)
        db.commit()
        for x in saved: db.refresh(x)
    return {"count":len(saved),"leads":[x.to_dict() for x in saved]}

@app.get("/api/leads")
def leads(limit:int=50):
    with SessionLocal() as db:
        return {"leads":[x.to_dict() for x in db.query(Lead).order_by(Lead.lead_score.desc().nullslast()).limit(limit).all()]}

@app.post("/api/analyze/website/{lead_id}")
def analyze(lead_id:int):
    with SessionLocal() as db:
        lead=db.get(Lead,lead_id)
        if not lead: raise HTTPException(404,"Lead not found")
        if not lead.website:
            lead.status=LeadStatus.NO_WEBSITE; db.commit(); return lead.to_dict()
        site=analyze_site(lead.website); ai=score(lead.to_dict(),site)
        lead.website_score=site["website_score"]; lead.analysis=site
        lead.lead_score=ai["lead_score"]; lead.problems=ai.get("problems",[])
        lead.opportunity=ai.get("opportunity","")
        lead.status=LeadStatus.QUALIFIED if lead.lead_score>=80 else LeadStatus.ANALYZED
        db.commit(); db.refresh(lead)
        if lead.status==LeadStatus.QUALIFIED:
            notify(f"🔥 QUALIFIED LEAD\n\n{lead.name}\nScore: {lead.lead_score}/100\n{lead.website}\n{lead.opportunity}\nProblems: {', '.join(lead.problems)}")
        return lead.to_dict(True)
