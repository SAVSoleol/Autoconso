from pathlib import Path

from streamlit.testing.v1 import AppTest

APP = Path(__file__).resolve().parents[1] / "app.py"


def get_dashboard(at):
    return next(h.proto.body for h in at.get("html") if 'class="pv-dashboard"' in h.proto.body)


def demo_app():
    at = AppTest.from_file(str(APP), default_timeout=30).run()
    next(b for b in at.button if b.label == "Voir un exemple").click().run()
    assert not at.exception
    assert not at.error
    return at


def test_empty_state_has_no_fake_results():
    at = AppTest.from_file(str(APP)).run()
    assert not at.exception
    assert '— %' in get_dashboard(at)


def test_demo_and_battery_toggle():
    at = demo_app()
    assert '41,7 %' in get_dashboard(at)
    assert 'Pertes de conversion' in get_dashboard(at)
    assert len(at.get("download_button")) == 2
    at.checkbox(key="simulate_battery").uncheck().run()
    assert not at.exception
    assert 'Pertes de conversion' not in get_dashboard(at)
    assert '50,0 %' in get_dashboard(at)


def test_existing_battery_is_never_simulated_twice():
    at = demo_app()
    at.checkbox(key="existing").check().run()
    assert not at.exception
    assert 'Autonomie énergétique apparente' in get_dashboard(at)
    assert 'Pertes de conversion' not in get_dashboard(at)
    assert at.slider(key="capacity").disabled


def test_edit_monthly_pv_and_restore_battery_settings():
    at = demo_app()
    january = at.number_input(key="pv_2025-01")
    original = january.value
    january.set_value(1.).run()
    assert not at.exception
    assert any('Janvier' in e.value for e in at.error)
    at.number_input(key="pv_2025-01").set_value(original).run()
    assert not at.error
    at.slider(key="capacity").set_value(25).run()
    next(b for b in at.button if b.label == "Réinitialiser la batterie").click().run()
    assert not at.exception
    assert at.slider(key="capacity").value == 15


def test_switching_back_to_own_data_clears_demo():
    at = demo_app()
    next(b for b in at.button if b.label == "Mes données").click().run()
    assert not at.exception
    assert '— %' in get_dashboard(at)
