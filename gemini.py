import os,json
from google import genai
def score(lead,site):
    key=os.getenv("GEMINI_API_KEY")
    if not key: raise RuntimeError("GEMINI_API_KEY missing")
    c=genai.Client(api_key=key)
    p=f'''You qualify prospects for a web design business. Use only supplied evidence.\nBUSINESS={json.dumps(lead)}\nWEBSITE={json.dumps(site)}\nReturn ONLY JSON: {{"lead_score":0,"opportunity":"high|medium|low","problems":[],"reason":""}}. Score 80+ only when there is a strong, visible website opportunity.'''
    t=c.models.generate_content(model=os.getenv("GEMINI_MODEL","gemini-2.5-flash"),contents=p).text.strip()
    t=t.replace("```json","").replace("```","").strip(); d=json.loads(t); d["lead_score"]=max(0,min(100,float(d.get("lead_score",0)))); return d
