import numpy as np
import pandas as pd
import pytest

from loaders import UnsupportedFormatError, load_meter_file, load_meter_files


@pytest.mark.parametrize("unit,factor", [("kWh", 1), ("Wh", 1000), ("kW", 4), ("W", 4000)])
def test_csv_units_and_end_timestamp(tmp_path, unit, factor):
    data = pd.DataFrame({"Date": ["2025-01-01T00:15:00+01:00", "2025-01-01T00:30:00+01:00"],
                         f"Import réseau ({unit})": [factor, 2 * factor],
                         f"Export réseau ({unit})": [0, 0]})
    path = tmp_path / "courbe.csv"
    data.to_csv(path, sep=";", decimal=",", index=False)
    f, m = load_meter_file(path)
    np.testing.assert_allclose(f.import_kWh, [1, 2])
    assert m.dt_hours == .25
    assert f.timestamp.iloc[0] == pd.Timestamp("2025-01-01T00:00:00+01:00")


def test_xlsx_with_metadata_rows(tmp_path):
    path = tmp_path / "compteur.xlsx"
    df = pd.DataFrame({"Date": ["01.01.2025 00:15", "01.01.2025 00:30"],
                       "Import réseau (kWh)": [1, 2], "Export réseau (kWh)": [0, .5]})
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, startrow=4)
    f, m = load_meter_file(path)
    assert f.import_kWh.sum() == 3
    assert f.export_kWh.sum() == .5


def test_two_files_do_not_allow_overlap(tmp_path):
    df = pd.DataFrame({"Date": ["01.01.2025 00:15", "01.01.2025 00:30"],
                       "Import réseau (kWh)": [1, 2], "Export réseau (kWh)": [0, 0]})
    p1, p2 = tmp_path / "a.csv", tmp_path / "b.csv"
    df.to_csv(p1, index=False)
    df.loc[1, "Import réseau (kWh)"] = 3
    df.to_csv(p2, index=False)
    with pytest.raises(UnsupportedFormatError, match="chevauchent"):
        load_meter_files([p1, p2], same_meter=True)


def test_unknown_cells_remain_unknown(tmp_path):
    path = tmp_path / "incomplet.csv"
    path.write_text("Date;Import réseau (kWh);Export réseau (kWh)\n01.01.2025 00:15;1;0\n01.01.2025 00:30;;0\n", encoding="utf-8")
    f, m = load_meter_file(path)
    assert np.isnan(f.import_kWh.iloc[1])
    assert not f.valid.iloc[1]
    assert m.completeness == .5
