"""DOSSIER collector v2. Pokretanje na svakih 10 min (cron / Railway / GitHub Actions).
pip install feedparser openai supabase
env: SUPABASE_URL, SUPABASE_SERVICE_KEY, OPENAI_API_KEY
SUPABASE_SERVICE_KEY je tajni service_role ključ. Nikad ga ne stavljaj u web aplikaciju.
Poštuj robots.txt i uslove korišćenja svakog izvora.
"""
import os, json, hashlib
from datetime import datetime, timezone
from time import mktime
import feedparser
from openai import OpenAI
from supabase import create_client

db = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_KEY"])
ai = OpenAI()

STAGES = ["privedjen", "hapsenje", "pritvor", "istraga", "optuznica", "osudjen", "obustavljeno", "oslobodjen"]

PROMPT = """Iz vesti izvuci podatke o krivičnom postupku u Srbiji. Vrati SAMO JSON:
{"relevant":bool,"person":str,"role":str,"institution":str,
 "stage":"privedjen|hapsenje|pritvor|istraga|optuznica|osudjen|obustavljeno|oslobodjen",
 "is_final":bool,"event_date":"YYYY-MM-DD ili null",
 "alleged_offense":str,"summary":str}
Značenje faza: privedjen = priveden/zadržan na ispitivanje bez hapšenja; hapsenje = uhapšen;
pritvor = određen pritvor; istraga = pokrenuta istraga; optuznica = podignuta optužnica;
osudjen = sud izrekao osuđujuću presudu (is_final=true samo ako je izvor kaže da je pravosnažna);
oslobodjen = oslobađajuća presuda; obustavljeno = postupak obustavljen.
Pravila: ne izmišljaj ništa; razlog navedi samo ako ga izvor navodi i kao tvrdnju organa
("sumnja se da..."); osudjen samo ako vest izričito govori o presudi suda; ako nije reč o
postupku organa (policija, tužilaštvo, sud) protiv nosioca javne funkcije ILI privatnog lica u vezi sa
korupcijom, privrednim kriminalom, zloupotrebom službenog položaja, izbornim malverzacijama ili
zloupotrebama vlasti, relevant=false. Optužbe medija ili političara bez formalnog koraka organa se ne računaju.
Najviše 2 rečenice u summary, neutralnim tonom, bez kvalifikacija koje izvor ne koristi.
event_date je datum samog događaja po vesti; ako nije jasan, null."""


def get_cutoff():
    v = db.table("config").select("value").eq("key", "cutoff_date").execute().data[0]["value"]
    return datetime.fromisoformat(v)


def classify(title, text):
    r = ai.chat.completions.create(
        model="gpt-4o-mini", temperature=0,
        response_format={"type": "json_object"},
        messages=[{"role": "system", "content": PROMPT},
                  {"role": "user", "content": f"{title}\n\n{text[:3000]}"}])
    return json.loads(r.choices[0].message.content)


def status_for(event_id):
    """1 zvanično (bar jedan zvaničan izvor), 2 više nezavisnih medija, 3 jedan izvor.
    Status 4 (opovrgnuto) postavlja samo čovek u admin ekranu i ovde se nikad ne prepisuje."""
    rows = (db.table("event_sources").select("source_id, sources(is_official)")
            .eq("event_id", event_id).execute().data)
    if any(r["sources"]["is_official"] for r in rows):
        return 1
    return 2 if len({r["source_id"] for r in rows}) >= 2 else 3


def upsert_person_case(d):
    name = d["person"].strip()
    ex = db.table("persons").select("id").ilike("full_name", name).execute().data
    if len(ex) == 1:
        p = ex[0]  # ista osoba već postoji u bazi, ne pravimo duplikat
    else:
        p = db.table("persons").upsert(
            {"full_name": name, "role": d.get("role"), "institution": d.get("institution")},
            on_conflict="full_name,institution").execute().data[0]
    c = db.table("cases").select("id").eq("person_id", p["id"]).execute().data
    if c:
        db.table("cases").update({"current_stage": d["stage"]}).eq("id", c[0]["id"]).execute()
        return p["id"], c[0]["id"]
    cid = db.table("cases").insert({"person_id": p["id"], "current_stage": d["stage"]}).execute().data[0]["id"]
    return p["id"], cid


def entry_time(e):
    t = e.get("published_parsed") or e.get("updated_parsed")
    return datetime.fromtimestamp(mktime(t), tz=timezone.utc) if t else None


def run():
    cutoff = get_cutoff()
    for s in db.table("sources").select("*").execute().data:
        if not str(s.get("feed_url", "")).startswith("http"):
            continue  # ručni izvori (manual:...) se ne čitaju automatski
        try:
            entries = feedparser.parse(s["feed_url"]).entries
        except Exception as ex:
            print("feed error", s["name"], ex)
            continue
        for e in entries:
            try:
                url = e.get("link")
                if not url:
                    continue
                t = entry_time(e)
                if t and t < cutoff:
                    continue  # starije od datuma promene vlasti
                if db.table("event_sources").select("url").eq("url", url).execute().data:
                    continue
                d = classify(e.get("title", ""), e.get("summary", ""))
                if not d.get("relevant") or not d.get("person") or d.get("stage") not in STAGES:
                    continue
                ev_date = d.get("event_date") or (t.date().isoformat() if t else None)
                if not ev_date:
                    continue
                if datetime.fromisoformat(ev_date).replace(tzinfo=cutoff.tzinfo) < cutoff:
                    continue
                person_id, case_id = upsert_person_case(d)
                h = hashlib.sha256(f'{d["person"]}|{d["stage"]}|{ev_date}'.lower().encode()).hexdigest()
                ev = db.table("events").select("id").eq("content_hash", h).execute().data
                if ev:
                    eid = ev[0]["id"]
                else:
                    eid = db.table("events").insert({
                        "case_id": case_id, "stage": d["stage"], "summary": d["summary"],
                        "alleged_offense": d.get("alleged_offense"), "is_final": bool(d.get("is_final")),
                        "event_time": ev_date, "content_hash": h,
                        "review_state": "pending"}).execute().data[0]["id"]  # ljudska provera
                db.table("event_sources").insert({"event_id": eid, "source_id": s["id"], "url": url}).execute()
                # Članak uz osobu (samo naslov, medij, datum, link). Urednik ga odobrava u adminu.
                db.table("person_articles").upsert({
                    "person_id": person_id, "outlet": s["name"], "title": (e.get("title") or "")[:300],
                    "url": url, "published_at": t.isoformat() if t else None,
                    "kind": s.get("article_kind", "news")}, on_conflict="person_id,url").execute()
                cur = db.table("events").select("status").eq("id", eid).execute().data[0]["status"]
                if cur != 4:
                    db.table("events").update({"status": status_for(eid)}).eq("id", eid).execute()
                # Push (Firebase) se šalje tek kad review_state postane 'approved'.
            except Exception as ex:
                print("entry error", url if 'url' in dir() else "?", ex)


if __name__ == "__main__":
    run()
