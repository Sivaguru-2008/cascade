from datetime import date, datetime, timedelta

from loop1.candidates import Ev, PatientState, step

EPOCH = date(2023, 1, 1)


def ev(day, drug, l4, prescriber="dr1", dose=10.0, ds=30):
    ts = datetime(2023, 1, 1, 10) + timedelta(days=day)
    return Ev(f"e{day}{drug}", ts, day, "PH001", "p1", prescriber, "70-74", "F", drug, l4, l4[:4], ds, dose)


def run(events):
    st = PatientState()
    return st, [step(st, e) for e in events]


def test_washout_blocks_incidence():
    _, res = run([ev(0, "X", "AAAAA"), ev(100, "B", "BBBBB")])
    assert not res[1].incident  # patient observed for < 365 days


def test_incident_when_class_not_seen_for_365_days():
    _, res = run([ev(0, "X", "AAAAA"), ev(400, "B", "BBBBB"), ev(420, "B", "BBBBB"), ev(800, "B", "BBBBB")])
    assert res[1].incident
    assert not res[2].incident  # same class 20 days earlier
    assert res[3].incident  # gap 380 > 365


def test_same_class_other_drug_is_not_incident():
    _, res = run([ev(0, "X", "AAAAA"), ev(380, "B1", "BBBBB"), ev(400, "B2", "BBBBB")])
    assert res[1].incident and not res[2].incident


def test_candidates_window_and_class():
    events = [ev(0, "X", "XXXXX"), ev(390, "A_old", "OOOOO"), ev(600, "A", "AAAAA"), ev(610, "A2", "BBBBB"),
              ev(640, "B", "CCCCC")]
    _, res = run(events)
    last = res[-1]
    assert last.incident
    names = {c.a.drug_id: c.days_since_a for c in last.candidates}
    assert names == {"A": 40, "A2": 30}  # A_old started 250 days ago (> 180); X older still
    assert "X" not in names


def test_same_day_start_is_not_a_candidate():
    _, res = run([ev(0, "X", "XXXXX"), ev(500, "A", "AAAAA"), ev(500, "B", "BBBBB")])
    assert res[-1].incident and res[-1].candidates == []


def test_state_hash_round_trip_and_dose_increase():
    st, res = run([ev(0, "X", "XXXXX"), ev(400, "A", "AAAAA", dose=5), ev(430, "A", "AAAAA", dose=10)])
    fields = {}
    for r in res:
        fields.update(r.fields)
    back = PatientState.from_hash(fields)
    assert back == st
    assert st.drugs["A"].dose_up_day == 430 and st.drugs["A"].prev_dose == 5 and st.drugs["A"].first_day == 400


def test_episode_restarts_after_long_gap():
    st, _ = run([ev(0, "X", "XXXXX"), ev(10, "A", "AAAAA"), ev(500, "A", "AAAAA")])
    assert st.drugs["A"].first_day == 500


def test_n_active_drugs_counts_recent_dispenses():
    _, res = run([ev(0, "X", "XXXXX"), ev(380, "A", "AAAAA"), ev(390, "C", "CCCCC"), ev(420, "B", "BBBBB")])
    # X last dispensed day 0 (inactive), A and C active, plus B itself
    assert res[-1].n_active_drugs == 3


def test_from_event_day():
    class E:
        event_id, ts, pharmacy_id, patient_token, prescriber_token = "e", datetime(2024, 1, 2, 9), "PH1", "t", "p"
        age_band, sex, drug_id, atc_l4, atc_l3, days_supply, dose_mg = "70-74", "F", "D", "AAAAA", "AAAA", 30, 5.0
    assert Ev.from_event(E, EPOCH).day == 366
