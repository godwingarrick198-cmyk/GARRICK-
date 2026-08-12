import os,re,time,httpx
OVERPASS=os.getenv("OVERPASS_API_URL","https://overpass-api.de/api/interpreter")
NOMINATIM=os.getenv("NOMINATIM_URL","https://nominatim.openstreetmap.org/search")
UA=os.getenv("OSM_USER_AGENT","GarrickAIOutreach/1.0")
MAP={"dentist":[("amenity","dentist")],"dentists":[("amenity","dentist")],"restaurant":[("amenity","restaurant")],"restaurants":[("amenity","restaurant")],"cafe":[("amenity","cafe")],"hotel":[("tourism","hotel")],"pharmacy":[("amenity","pharmacy")],"gym":[("leisure","fitness_centre")],"beauty salon":[("shop","beauty")],"hair salon":[("shop","hairdresser")]}
def norm(u):
    if not u:return None
    return u if re.match(r"^https?://",u,re.I) else "https://"+u
def search_businesses(niche,city,limit):
    h={"User-Agent":UA,"Accept":"application/json"}
    with httpx.Client(timeout=30,headers=h) as c:
        r=c.get(NOMINATIM,params={"q":city,"format":"jsonv2","limit":1}); r.raise_for_status(); loc=r.json()[0]
        time.sleep(1)
        tags=MAP.get(niche.lower(),[("name",niche)])
        clauses=[f'nwr["{k}"="{v}"](around:15000,{loc["lat"]},{loc["lon"]});' for k,v in tags]
        q="[out:json][timeout:25];("+''.join(clauses)+");out center tags;"
        r=c.post(OVERPASS,content=q,headers={"Content-Type":"text/plain"}); r.raise_for_status()
        out=[]; seen=set()
        for e in r.json().get("elements",[]):
            t=e.get("tags",{}); name=t.get("name"); sid=f'{e["type"]}/{e["id"]}'
            if not name or sid in seen:continue
            seen.add(sid); center=e.get("center",{})
            out.append({"source_id":sid,"name":name,"category":t.get("amenity") or t.get("shop") or t.get("tourism") or niche,"website":norm(t.get("website") or t.get("contact:website")),"phone":t.get("phone") or t.get("contact:phone"),"address":t.get("addr:full"),"city":t.get("addr:city") or city,"country":t.get("addr:country"),"latitude":e.get("lat",center.get("lat")),"longitude":e.get("lon",center.get("lon"))})
            if len(out)>=limit:break
        return out
