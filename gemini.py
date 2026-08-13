import os, json
from google import genai


def score(lead, site):
    key = os.getenv("GEMINI_API_KEY")
    if not key:
        raise RuntimeError("GEMINI_API_KEY missing")

    c = genai.Client(api_key=key)

    p = f'''You qualify prospects for a web design business. Use only supplied evidence.
BUSINESS={json.dumps(lead)}
WEBSITE={json.dumps(site)}
Return ONLY JSON: {{"lead_score":0,"opportunity":"high|medium|low","problems":[],"reason":""}}.
Score 80+ only when there is a strong, visible website opportunity.'''

    model = os.getenv("GEMINI_MODEL", "gemini-3.6-flash")

    try:
        response = c.models.generate_content(
            model=model,
            contents=p
        )

        if not response.text:
            raise RuntimeError("Gemini returned an empty response")

        t = response.text.strip()
        t = t.replace("```json", "").replace("```", "").strip()

        d = json.loads(t)
        d["lead_score"] = max(
            0,
            min(100, float(d.get("lead_score", 0)))
        )

        return d

    except Exception as e:
        raise RuntimeError(f"Gemini scoring failed: {type(e).__name__}") from e
