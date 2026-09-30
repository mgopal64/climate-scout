"""Offline tests - no network. Run: python -m pytest -q"""
import json

import pytest

from scout import ats, config, filters, getro, llm, pipeline, poll
from scout.discover import guess, similar, slug_candidates

FCFG = config.load()["filters"]


# ----------------------------------------------------------------- detection
@pytest.mark.parametrize("url,expected", [
    ("https://boards.greenhouse.io/watershed/jobs/123", ("greenhouse", "watershed")),
    ("https://job-boards.greenhouse.io/antora/jobs/9", ("greenhouse", "antora")),
    ("https://job-boards.eu.greenhouse.io/octopus/jobs/1", ("greenhouse", "octopus")),
    ("https://boards.greenhouse.io/embed/job_board?for=fervo&b=x", ("greenhouse", "fervo")),
    ("https://jobs.lever.co/paces/abc-123", ("lever", "paces")),
    ("https://jobs.eu.lever.co/kayrros/xyz", ("lever_eu", "kayrros")),
    ("https://jobs.ashbyhq.com/brightband/5f3c", ("ashby", "brightband")),
    ("https://jobs.ashbyhq.com/Salient%20Predictions/1", ("ashby", "Salient Predictions")),
    ("https://apply.workable.com/gigaton/j/ABC/", ("workable", "gigaton")),
    ("https://jobs.smartrecruiters.com/Siemens/7440", ("smartrecruiters", "Siemens")),
    ("https://ecolab.wd1.myworkdayjobs.com/en-US/Ecolab_External/job/1", ("workday", "ecolab|Ecolab_External")),
    ("https://apply.workable.com/j/ABC123", None),
    ("https://example.com/careers", None),
    ("", None),
])
def test_detect(url, expected):
    assert ats.detect_ats(url) == expected


def test_find_all_in_next_data():
    page = ('<script id="__NEXT_DATA__">{"a":"https:\\/\\/jobs.lever.co\\/paces",'
            '"b":"https://boards.greenhouse.io/embed/job_board?for=fervo",'
            '"c":"https://jobs.ashbyhq.com/watershed/1"}</script>')
    assert ats.find_all_ats(page) == {("lever", "paces"), ("greenhouse", "fervo"),
                                      ("ashby", "watershed")}


def test_strip_html_greenhouse_double_escaped():
    s = "&lt;p&gt;3+ years of experience&lt;/p&gt;&lt;ul&gt;&lt;li&gt;Python &amp;amp; SQL&lt;/li&gt;"
    out = ats.strip_html(s)
    assert "3+ years of experience" in out and "Python & SQL" in out and "<" not in out


# ----------------------------------------------------------------- normalizers
def test_normalizers(monkeypatch):
    payloads = {
        "boards-api.greenhouse.io/v1/boards/w/jobs": {"jobs": [
            {"id": 1, "title": "ML Engineer", "location": {"name": "Remote, US"},
             "absolute_url": "https://x/1", "first_published": "2026-09-01"}]},
        "api.lever.co/v0/postings/p": [
            {"id": "a", "text": "Software Engineer", "categories": {"location": "NYC"},
             "hostedUrl": "https://l/a", "createdAt": 1790000000000,
             "descriptionPlain": "Build grid software",
             "lists": [{"text": "Req", "content": "<li>1+ years of experience</li>"}]}],
        "api.ashbyhq.com/posting-api/job-board/b": {"jobs": [
            {"id": "z", "title": "Research Engineer", "location": "SF",
             "secondaryLocations": [{"location": "NYC"}], "isRemote": True,
             "jobUrl": "https://a/z", "publishedAt": "2026-09-02", "descriptionPlain": "x"},
            {"id": "hidden", "title": "Hidden", "isListed": False}]},
    }

    def fake(url, **kw):
        for k, v in payloads.items():
            if k in url:
                return v
        raise AssertionError(url)

    monkeypatch.setattr(ats.http, "get_json", fake)
    g = ats.fetch_jobs("greenhouse", "w", "W")[0]
    assert (g.title, g.location, g.key) == ("ML Engineer", "Remote, US", "greenhouse:w:1")
    lv = ats.fetch_jobs("lever", "p", "Paces")[0]
    assert "1+ years of experience" in lv.description and lv.posted_at.startswith("2026")
    ab = ats.fetch_jobs("ashby", "b", "B")
    assert len(ab) == 1 and ab[0].location == "SF / NYC / Remote"


# ----------------------------------------------------------------- getro
def test_getro_network_id_candidates():
    html = ('<script id="__NEXT_DATA__" type="application/json">'
            + json.dumps({"props": {"pageProps": {"network": {"name": "MCJ", "id": "4321"}}}})
            + "</script>")
    assert getro.extract_candidates(html) == [4321]                     # string id
    assert getro.extract_candidates('"network":{"slug":"x","name":"y","id":77}') == [77]
    assert getro.extract_candidates('{"collectionId":"12"}') == [12]
    assert getro.extract_candidates('<img src="/collections/2/logo.png">') == []   # no junk
    assert getro.extract_candidates('fetch("https://api.getro.com/api/v2/collections/99/x")') == [99]


def test_getro_resolve_validates(monkeypatch):
    page = '"network":{"id":2} ... "networkId":"4321"'
    monkeypatch.setattr(getro.http, "get_text", lambda url: page)
    monkeypatch.setattr(getro, "probe", lambda net, b: (None, "404") if net == 2 else (500, "ok"))
    assert getro.resolve_network_id("https://b") == 4321
    monkeypatch.setattr(getro, "probe", lambda net, b: (None, "404"))
    with pytest.raises(RuntimeError, match="2:404"):
        getro.resolve_network_id("https://b")


def test_getro_walk_paginates(monkeypatch):
    calls = []

    def fake_post(url, payload, **kw):
        calls.append(payload["page"])
        assert kw["headers"]["Accept"] == "application/json"
        n = 20 if payload["page"] < 2 else 5
        return {"results": {"count": 45, "jobs": [{"id": i} for i in range(n)]}}

    monkeypatch.setattr(getro.http, "post_json", fake_post)
    monkeypatch.setattr(getro, "PAUSE", 0)
    assert len(getro.walk_jobs(1, "https://b")) == 45 and calls == [0, 1, 2]


def test_getro_to_job_and_badges():
    j = getro.to_job({"id": 5, "slug": "ml-eng", "title": "ML EngineerNewRemote",
                      "organization": {"name": " Form  Energy ", "slug": "form"},
                      "locations": ["Boston, MA, USA"], "created_at": 1790000000},
                     "https://jobs.x.org/")
    assert j.title == "ML Engineer" and j.company == "Form Energy"
    assert j.url == "https://jobs.x.org/companies/form/jobs/ml-eng"


# ----------------------------------------------------------------- filters
@pytest.mark.parametrize("title,ok", [
    ("Machine Learning Engineer", True),
    ("Software Engineer, New Grad", True),
    ("Data Scientist - Grid Forecasting", True),
    ("Senior Software Engineer", False),
    ("Staff ML Engineer", False),
    ("Engineering Manager", False),
    ("Software Engineering Intern", False),
    ("Account Executive", False),
    ("Field Service Technician", False),
    ("Office Coordinator", False),
])
def test_titles(title, ok):
    assert filters.title_verdict(title, FCFG)[0] is ok


@pytest.mark.parametrize("loc,ok", [
    ("Remote, US", True), ("Ann Arbor, MI", True), ("New York, NY", True),
    ("London, United Kingdom", True), ("Remote", True), ("", True),
    ("Indianapolis, IN", True), ("Bengaluru, Karnataka, India", False),
    ("Chennai, Tamil Nadu, India", False), ("Toronto, Canada", False),
    ("Berlin, Germany", False), ("Remote - EMEA", False),
    ("United States; Remote", True), ("Houston, TX, USA", True),
])
def test_locations(loc, ok):
    assert filters.location_ok(loc, FCFG)[0] is ok


def test_uk_toggle():
    assert filters.location_ok("London, UK", {**FCFG, "allow_uk": False})[0] is False


@pytest.mark.parametrize("desc,years", [
    ("You have 5+ years of experience building distributed systems.", 5),
    ("Requirements: 3-5 years of professional experience", 3),
    ("minimum of three years of relevant experience", 3),
    ("Experience: 1+ years with Python", 1),
    ("We were founded 10 years ago.", None),
])
def test_required_years(desc, years):
    assert filters.required_years(desc) == years


def test_description_verdicts():
    ok, reason, _ = filters.description_verdict("5+ years of experience in C++", FCFG)
    assert not ok and "5" in reason                      # the Tesla failure mode
    ok, _, flags = filters.description_verdict(
        "Candidates must have the right to work in the UK. 1+ years of experience.", FCFG)
    assert ok and "UK right-to-work required" in flags   # the Gigaton failure mode
    ok, _, flags = filters.description_verdict(
        "New grad role! 3+ years of experience a plus.", FCFG)
    assert ok and "new-grad language in posting" in flags
    ok, _, flags = filters.description_verdict("2+ years of experience. We cannot sponsor visas.", FCFG)
    assert ok and "asks for 2+ yrs" in flags and "no visa sponsorship" in flags


# ----------------------------------------------------------------- llm parsing
def test_parse_score():
    s = llm.parse_score('```json\n{"fit": 14, "level": "Early", "knockouts": ["PhD required"],'
                        ' "reason": "grid ML"}\n```')
    assert s == {"fit": 10, "level": "early", "knockouts": ["PhD required"], "reason": "grid ML"}
    assert llm.parse_score('{"fit": 3, "level": "weird"}')["level"] == "unclear"
    with pytest.raises(ValueError):
        llm.parse_score("sorry, I can't")


def test_llm_skipped_without_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    j = ats.Job("ashby", "b", "B", "1", "ML Engineer", "Remote", "u")
    assert llm.score(j, "profile", {}) is None


# ----------------------------------------------------------------- discovery helpers
def test_slug_candidates_and_similarity():
    assert slug_candidates("Salient Predictions, Inc.") == ["salientpredictions", "salient-predictions"]
    assert similar("Watershed Technology", "Watershed")
    assert not similar("Arcadia Power", "Pacific Gas")


def test_guess_rejects_greenhouse_name_mismatch(monkeypatch):
    monkeypatch.setattr(ats, "probe_greenhouse", lambda s: "Totally Different Co")
    monkeypatch.setattr(ats, "has_jobs", lambda a, s: a == "lever" and s == "paces")
    assert guess("Paces") == ("lever", "paces")


def test_contacts_match():
    c = {"Paces": ["James"], "Watershed": ["Kevin"]}
    assert pipeline.contacts_for("Paces Inc", c) == ["James"]
    assert pipeline.contacts_for("Form Energy", c) == []


# ----------------------------------------------------------------- end to end poll
def test_poll_baseline_then_alert(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "STATE", tmp_path)
    monkeypatch.setattr(config, "load_companies",
                        lambda: [{"ats": "ashby", "slug": "paces", "name": "Paces", "sources": []}])
    monkeypatch.setattr(config, "load_contacts", lambda: {"Paces": ["James"]})
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("NTFY_TOPIC", raising=False)
    listing = [ats.Job("ashby", "paces", "Paces", "1", "Senior Engineer", "NYC", "u1")]
    monkeypatch.setattr(ats, "fetch_jobs", lambda a, s, c: list(listing))
    sent = []
    monkeypatch.setattr(poll, "notify_batch", lambda items, c, cfg, **k: sent.extend(items))

    poll.main([])                                  # run 1: baseline silently
    assert sent == [] and "ashby:paces" in config.State.load().initialized

    listing.append(ats.Job("ashby", "paces", "Paces", "2", "Software Engineer, Grid",
                           "New York, NY", "u2", description="0-2 years of experience."))
    listing.append(ats.Job("ashby", "paces", "Paces", "3", "Sales Lead", "NYC", "u3"))
    poll.main([])                                  # run 2: only the matching new job
    assert [j.job_id for j, _ in sent] == ["2"]
    assert "new-grad language in posting" in sent[0][1].flags

    poll.main([])                                  # run 3: nothing new
    assert len(sent) == 1


# ----------------------------------------------------------------- end to end discover
def test_discover_end_to_end(monkeypatch, tmp_path):
    from scout import discover
    monkeypatch.setattr(config, "DATA", tmp_path)
    monkeypatch.setattr(config, "STATE", tmp_path)
    monkeypatch.setattr(config, "load", lambda: {
        "getro_boards": [{"name": "BoardA", "url": "https://a"}, {"name": "BoardB", "url": "https://b"}],
        "page_sources": [{"name": "CTL", "urls": ["https://ctl"]}],
        "discovery": {"guess_workers": 2}, "filters": FCFG,
        "llm": {"enabled": False}, "notify": {"ntfy_server": "https://ntfy.sh"}})
    monkeypatch.setattr(config, "load_seeds", lambda: [{"name": "Brightband"}, {"name": "Nowhere Co"}])
    monkeypatch.setattr(getro, "resolve_network_id", lambda url: 1 if url.endswith("a") else 2)
    board_jobs = {
        1: [{"id": 1, "title": "ML Engineer", "url": "https://jobs.lever.co/paces/x",
             "organization": {"name": "Paces", "slug": "paces"}},
            {"id": 2, "title": "Data Scientist", "url": "https://ecolab.wd1.myworkdayjobs.com/Ext/job/1",
             "organization": {"name": "BigCo", "slug": "bigco"}, "locations": ["Remote, US"]}],
        2: [{"id": 3, "title": "Engineer", "url": "https://jobs.lever.co/paces/y",
             "organization": {"name": "Paces", "slug": "paces"}},
            {"id": 4, "title": "Research Engineer", "url": "https://formenergy.com/careers",
             "organization": {"name": "Form Energy", "slug": "form"}}],
    }
    walks = {"n": 0}

    def fake_walk(net, url, max_pages=0):
        walks["n"] += 1
        return list(board_jobs[net])

    monkeypatch.setattr(getro, "walk_jobs", fake_walk)
    monkeypatch.setattr(discover.sources, "extract_from_pages",
                        lambda urls: ([("greenhouse", "watershed"), ("workday", "x|y")], []))
    monkeypatch.setattr(ats, "probe_greenhouse", lambda s: "Form Energy" if s == "formenergy" else None)
    monkeypatch.setattr(ats, "has_jobs", lambda a, s: a == "ashby" and s == "brightband")
    monkeypatch.setattr(ats, "board_gone", lambda a, s: False)
    sent = []
    monkeypatch.setattr(discover, "notify_batch", lambda items, c, cfg, **k: sent.extend(j for j, v in items))

    discover.main([])
    cos = {(c["ats"], c["slug"]): c for c in json.loads((tmp_path / "companies.json").read_text())}
    assert set(cos) == {("lever", "paces"), ("greenhouse", "watershed"),
                        ("greenhouse", "formenergy"), ("ashby", "brightband")}
    assert cos[("lever", "paces")]["sources"] == ["BoardA", "BoardB"]
    assert cos[("greenhouse", "formenergy")]["guessed"] is True
    report = (tmp_path / "discovery_report.md").read_text()
    assert "Nowhere Co" in report and "workday" in report
    assert sent == []                               # first run baselines the catch-all

    board_jobs[1].append({"id": 9, "title": "Machine Learning Engineer",
                          "url": "https://ecolab.wd1.myworkdayjobs.com/Ext/job/9",
                          "organization": {"name": "BigCo", "slug": "bigco"},
                          "locations": ["Houston, TX, USA"]})
    discover.main([])                               # second run: new Workday job via catch-all
    assert [j.company for j in sent] == ["BigCo"]
    assert len(json.loads((tmp_path / "companies.json").read_text())) == 4


# ----------------------------------------------------------------- consider (MCJ)
MCJ_JOBS = [  # trimmed from a real jobs.mcj.vc response
    {"jobId": "52f4", "companyName": "Crusoe", "title": "Senior Performance Engineer",
     "url": "https://jobs.ashbyhq.com/Crusoe/52f4",
     "applyUrl": "https://jobs.ashbyhq.com/Crusoe/52f4?utm_source=jobs.mcj.vc",
     "locations": ["San Francisco, California, USA"], "remote": False,
     "jobSeniorityIds": ["senior"], "timeStamp": "2026-09-28T00:00:00Z"},
    {"jobId": "62ab", "companyName": "Charm Industrial", "title": "Shipper/Reciever",
     "url": "https://jobs.lever.co/charmindustrial/62ab", "locations": ["Fort Lupton, CO"]},
    {"jobId": "112794", "companyName": "Charge Robotics", "title": "Robotics Software Engineer",
     "url": "https://www.workatastartup.com/jobs/112794", "locations": ["San Leandro HQ"],
     "minYearsExp": 5, "jobSeniorityIds": ["mid"]},
]


def test_consider_walk_paginates(monkeypatch):
    from scout import consider
    seqs = []

    def fake_post(self, payload):
        assert self.board == "https://jobs.mcj.vc"
        assert payload["board"] == {"id": "mcj", "isParent": True}
        assert set(payload) == {"meta", "board", "query"}        # no extra keys (412 otherwise)
        seqs.append(payload["meta"].get("sequence"))
        page = len(seqs) - 1
        return {"jobs": MCJ_JOBS if page < 2 else MCJ_JOBS[:1],
                "meta": {"size": 30, "sequence": f"c{page + 1}"}, "total": 7}

    monkeypatch.setattr(consider.Session, "post", fake_post)
    monkeypatch.setattr(consider, "PAUSE", 0)
    assert len(consider.walk_jobs("https://jobs.mcj.vc/", "mcj")) == 7
    assert seqs == [None, "c1", "c2"]


def test_consider_csrf_from_cookie_and_meta(monkeypatch):
    from scout import consider

    class Resp:
        def __init__(self, text): self.text, self.status_code = text, 200
        def raise_for_status(self): pass

    sess = consider.Session("https://jobs.mcj.vc/")
    monkeypatch.setattr(sess.s, "get", lambda url, **k: (
        sess.s.cookies.set("csrf_token", "TOK", domain="jobs.mcj.vc"), Resp("<html></html>"))[1])
    sess.prime()
    assert sess.token == "TOK" and sess.headers()["X-CSRF-Token"] == "TOK"
    assert "climate-scout" not in sess.s.headers["User-Agent"]

    sess2 = consider.Session("https://jobs.mcj.vc")
    monkeypatch.setattr(sess2.s, "get", lambda url, **k: Resp(
        '<meta name="csrf-token" content="META123">'))
    sess2.prime()
    assert sess2.token == "META123"


def test_consider_reprimes_on_412(monkeypatch):
    from scout import consider
    codes = [412, 200]
    primes = []

    class R:
        def __init__(self, c): self.status_code = c
        def raise_for_status(self):
            if self.status_code >= 400:
                raise consider.requests.HTTPError(str(self.status_code), response=self)
        def json(self): return {"jobs": [], "meta": {}, "total": 0}

    sess = consider.Session("https://jobs.mcj.vc")
    sess.cookie_names = ["already-primed"]
    monkeypatch.setattr(sess, "prime", lambda: primes.append(1))
    monkeypatch.setattr(sess.s, "post", lambda *a, **k: R(codes.pop(0)))
    assert sess.post({"meta": {}})["jobs"] == []
    assert primes == [1]


def test_consider_to_job_feeds_years_filter():
    from scout import consider
    j = consider.to_job(MCJ_JOBS[2])
    assert (j.company, j.title, j.job_id) == ("Charge Robotics", "Robotics Software Engineer", "112794")
    ok, reason, _ = filters.description_verdict(j.description, FCFG)
    assert not ok and "5" in reason                    # structured minYearsExp is enforced
    assert "Seniority: senior" in consider.to_job(MCJ_JOBS[0]).description


def test_discover_consider_board_and_late_baseline(monkeypatch, tmp_path):
    from scout import consider, discover
    monkeypatch.setattr(config, "DATA", tmp_path)
    monkeypatch.setattr(config, "STATE", tmp_path)
    boards = {"consider_boards": []}
    monkeypatch.setattr(config, "load", lambda: {
        "getro_boards": [], "page_sources": [], **boards,
        "discovery": {"guess_workers": 2}, "filters": FCFG, "llm": {"enabled": False},
        "poll": {"max_notifications_per_run": 15}, "notify": {"ntfy_server": "https://ntfy.sh"}})
    monkeypatch.setattr(config, "load_seeds", lambda: [])
    monkeypatch.setattr(ats, "probe_greenhouse", lambda s: None)
    monkeypatch.setattr(ats, "has_jobs", lambda a, s: False)
    monkeypatch.setattr(ats, "board_gone", lambda a, s: False)
    mcj = list(MCJ_JOBS)
    monkeypatch.setattr(consider, "walk_jobs", lambda url, bid: list(mcj))
    sent = []
    monkeypatch.setattr(discover, "notify_batch", lambda items, c, cfg, **k: sent.extend(j for j, v in items))

    discover.main(["--no-guess"])                  # no MCJ yet
    boards["consider_boards"] = [{"name": "MCJ", "url": "https://jobs.mcj.vc", "board_id": "mcj"}]
    discover.main(["--no-guess"])                  # MCJ added later: silent baseline
    cos = {(c["ats"], c["slug"]) for c in json.loads((tmp_path / "companies.json").read_text())}
    assert cos == {("ashby", "Crusoe"), ("lever", "charmindustrial")}
    assert sent == []

    mcj.append({"jobId": "999", "companyName": "Charge Robotics", "title": "Software Engineer, New Grad",
                "url": "https://www.workatastartup.com/jobs/999", "locations": ["San Leandro, CA"]})
    mcj.append({"jobId": "998", "companyName": "Charge Robotics", "title": "Controls Engineer",
                "url": "https://www.workatastartup.com/jobs/998", "locations": ["San Leandro, CA"],
                "minYearsExp": 6})
    discover.main(["--no-guess"])                  # only the new-grad role alerts
    assert [j.title for j in sent] == ["Software Engineer, New Grad"]


# ----------------------------------------------------------------- free Gemini provider
def test_gemini_provider(monkeypatch):
    calls = []

    class R:
        status_code = 200
        def raise_for_status(self): pass
        def json(self):
            return {"candidates": [{"content": {"parts": [
                {"text": '{"fit": 8, "level": "early", "knockouts": [], "reason": "grid ML"}'}]}}]}

    def fake_post(url, headers=None, json=None, timeout=None):
        calls.append((url, headers))
        return R()

    monkeypatch.setattr(llm.requests, "post", fake_post)
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    monkeypatch.setitem(llm._state, "fails", 0)
    cfg = {"provider": "gemini", "model": "gemini-flash-latest", "rpm": 0}
    assert llm.available(cfg)
    j = ats.Job("ashby", "g", "GridCARE", "1", "Optimization Engineer", "CA", "u")
    assert llm.score(j, "p", cfg)["fit"] == 8
    assert "models/gemini-flash-latest:generateContent" in calls[0][0]
    assert calls[0][1]["x-goog-api-key"] == "k"


def test_quota_breaker(monkeypatch):
    class R429:
        status_code = 429
        def raise_for_status(self):
            raise llm.requests.HTTPError("429", response=self)

    n = {"calls": 0}

    def fake_post(*a, **k):
        n["calls"] += 1
        return R429()

    monkeypatch.setattr(llm.requests, "post", fake_post)
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    monkeypatch.setitem(llm._state, "fails", 0)
    cfg = {"provider": "gemini", "rpm": 0}
    j = ats.Job("ashby", "g", "G", "1", "ML Engineer", "CA", "u")
    for _ in range(6):
        assert llm.score(j, "p", cfg) is None
    assert n["calls"] == llm.MAX_429            # stops calling after the breaker trips
    monkeypatch.setitem(llm._state, "fails", 0)


def test_rules_only_without_keys(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert not llm.available({"provider": "gemini"})
    assert not llm.available({"provider": "anthropic"})


# ----------------------------------------------------------------- email digest
def test_email_digest(monkeypatch):
    from scout import pipeline
    sent = []

    class FakeSMTP:
        def __init__(self, host, port, timeout=None): sent.append(("host", host, port))
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def login(self, u, p): sent.append(("login", u))
        def send_message(self, m): sent.append(("msg", m))

    monkeypatch.setattr(pipeline.smtplib, "SMTP_SSL", FakeSMTP)
    monkeypatch.setenv("EMAIL_USER", "me@gmail.com")
    monkeypatch.setenv("EMAIL_APP_PASSWORD", "x")
    monkeypatch.delenv("EMAIL_TO", raising=False)
    cfg = {"notify": {"channels": ["email"]}}
    a = (ats.Job("ashby", "x", "Form Energy", "1", "Data Scientist", "Boston, MA", "https://a"),
         pipeline.Verdict(True, fit=7, level="early", why="ok"))
    b = (ats.Job("lever", "paces", "Paces", "2", "Software Engineer <Grid>", "NYC", "https://b"),
         pipeline.Verdict(True, fit=6, level="early", why="grid"))
    pipeline.notify_batch([a, b], {"Paces": ["James"]}, cfg, overflow=3)
    msg = [m for kind, *m in sent if kind == "msg"][0][0]
    assert msg["To"] == "me@gmail.com"
    assert msg["Subject"].startswith("[climate-scout] 2 new roles: Software Engineer <Grid> @ Paces")
    body = msg.get_body(("html",)).get_content()
    assert "You know:</b> James" in body and "&lt;Grid&gt;" in body and "+3 more" in body
    assert body.index("Paces") < body.index("Form Energy")          # contacts first


def test_email_skipped_without_creds(monkeypatch, capsys):
    from scout import pipeline
    monkeypatch.delenv("EMAIL_USER", raising=False)
    monkeypatch.delenv("EMAIL_APP_PASSWORD", raising=False)
    assert pipeline.send_email("s", "t", "<p>h</p>", {}) is False
    pipeline.notify_batch([], {}, {})                                 # empty run sends nothing


# ----------------------------------------------------------------- robustness fixes
def test_llm_retries_transient_503(monkeypatch):
    seq = [503, 200]

    class R:
        def __init__(self, code): self.status_code = code
        def raise_for_status(self):
            if self.status_code >= 400:
                raise llm.requests.HTTPError(str(self.status_code), response=self)
        def json(self):
            return {"candidates": [{"content": {"parts": [
                {"text": '{"fit": 7, "level": "early", "knockouts": [], "reason": "ok"}'}]}}]}

    monkeypatch.setattr(llm.requests, "post", lambda *a, **k: R(seq.pop(0)))
    monkeypatch.setattr(llm, "RETRY_SLEEP", 0)
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    monkeypatch.setitem(llm._state, "fails", 0)
    j = ats.Job("ashby", "g", "GridCARE", "1", "Physical Systems Modeling Engineer", "CA", "u")
    assert llm.score(j, "p", {"provider": "gemini", "rpm": 0})["fit"] == 7


def test_board_gone_only_on_404(monkeypatch):
    def fake(ats_, slug, company):
        if slug == "gone":
            raise ats.http.NotFound("x")
        if slug == "flaky":
            raise RuntimeError("timeout")
        return []
    monkeypatch.setattr(ats, "fetch_jobs", fake)
    assert ats.board_gone("ashby", "gone") is True
    assert ats.board_gone("ashby", "flaky") is False
    assert ats.board_gone("ashby", "ok") is False