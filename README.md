# Oracle Client for macOS

A simple, lightweight Oracle SQL client for macOS. No Oracle Instant Client, no heavyweight IDE — just connect and query.

Built with Python + Tkinter + [python-oracledb](https://oracle.github.io/python-oracledb/) (thin mode), so it connects to Oracle directly over the network without any Oracle client libraries installed.

> 🇹🇷 Türkçe özet için [aşağıya](#türkçe) bakın.

## Features

- **Connect** with host, port, service name/SID, username and password
- **Connection profiles** — save and reuse connection details (password saving is opt-in)
- **Object browser** — schema → tables/views → object tree with lazy loading and instant filtering
- **Top-100 preview** — double-click any table or view to load its first 100 rows into the grid
- **SQL editor** with syntax highlighting (keywords, strings, comments, numbers)
- **Autocomplete / IntelliSense** — SQL keywords, schema names, table/view names, and column suggestions after typing `TABLE_NAME.` (Ctrl+Space to trigger manually)
- **Query results grid** with horizontal/vertical scrolling (first 1000 rows for ad-hoc queries)
- **CSV export** of query results (semicolon-separated, UTF-8 BOM — opens directly in Excel)
- **Query history** — every executed query is kept with a timestamp, double-click to reload (persists across restarts)
- **Script files** — open and save `.sql` scripts (⌘S)
- **Non-blocking UI** — queries run on a background thread
- DML/DDL statements are executed and committed, with affected row count reported

## Requirements

- macOS (tested on Apple Silicon and Intel)
- Python 3.9+ with Tkinter
  - **Important:** Tcl/Tk **8.6.13 or newer** is required on recent macOS versions. Older Tk 8.6.12 (shipped with some Anaconda/python.org builds) has a known bug where buttons don't respond to clicks until the window is moved.
  - Check yours: `python3 -c "import tkinter; print(tkinter.Tcl().call('info','patchlevel'))"`
  - Anaconda users can upgrade with: `conda install 'tk>=8.6.13'`
- An Oracle database reachable over the network (11g and newer; thin mode supports 12.1+ for most features, `ROWNUM` is used for previews so older versions work too)

## Installation

```bash
git clone https://github.com/kadirevciler/oracle-client-macos.git
cd oracle-client-macos
pip3 install -r requirements.txt
python3 oracle_client.py
```

### Build a standalone .app (optional)

To get a double-clickable macOS application that bundles Python and all dependencies:

```bash
pip3 install pyinstaller
python3 -m PyInstaller --noconfirm --windowed --name "Oracle Client" oracle_client.py
```

The app appears under `dist/Oracle Client.app` — drag it to your Desktop or `/Applications`.

If macOS Gatekeeper warns on first launch, right-click the app and choose **Open** once.

## Usage notes

- **Keyboard shortcuts:** ⌘↩ or F5 = run query · ⌘S = save script · ⌘O = open script · Ctrl+Space = autocomplete · Esc = close suggestion popup
- Ad-hoc queries display the first 1000 rows; table/view previews load the first 100 rows.
- Config files live in your home directory: `~/.oracle_client_history.json` (query history) and `~/.oracle_client_profiles.json` (connection profiles).
- ⚠️ If you tick *"Şifreyi de kaydet"* (save password), the password is stored **base64-encoded, not encrypted**, in the profiles file. Don't use it on shared machines.

## Contributing

Contributions are very welcome — this project exists so that nobody has to hunt for a simple Oracle client again, and the hope is that the community will make it much better. Ideas that would be great to have:

- Column sorting in the results grid
- Multiple result tabs / multiple connections
- Explain plan viewer
- DDL viewer (show `CREATE` statement of a table/view)
- Dark mode
- Homebrew cask / signed releases

Fork it, open an issue, send a PR. Everything is in a single file ([oracle_client.py](oracle_client.py)) on purpose — easy to read, easy to hack.

## License

[MIT](LICENSE)

---

## Türkçe

macOS için basit ve hafif bir Oracle SQL istemcisi. Oracle Instant Client kurulumu gerektirmez; Python + Tkinter + python-oracledb (thin mode) ile doğrudan bağlanır.

**Özellikler:** bağlantı profilleri, şema → tablo/görünüm ağacı (çift tık = ilk 100 satır), sözdizimi renklendirmeli SQL editörü, otomatik tamamlama (`TABLO.` yazınca kolon önerisi), CSV dışa aktarma, kalıcı sorgu tarihçesi, `.sql` betik aç/kaydet.

**Kurulum:**

```bash
git clone https://github.com/kadirevciler/oracle-client-macos.git
cd oracle-client-macos
pip3 install -r requirements.txt
python3 oracle_client.py
```

**Önemli:** Yeni macOS sürümlerinde Tcl/Tk **8.6.13+** gerekir (8.6.12'de düğmeler pencere taşınana kadar tıklamaya yanıt vermez). Anaconda kullanıyorsanız: `conda install 'tk>=8.6.13'`

Çift tıklanabilir bağımsız `.app` üretmek için yukarıdaki PyInstaller adımlarını izleyin. Katkılarınızı bekliyoruz!
