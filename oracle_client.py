#!/usr/bin/env python3
"""Basit Oracle SQL istemcisi — macOS masaüstü uygulaması.

Gereksinim: pip3 install oracledb  (thin mode, Oracle Instant Client gerekmez)
Çalıştırma: python3 oracle_client.py
"""

import base64
import csv
import decimal
import html
import json
import os
import queue
import re
import threading
import time
import webbrowser
from datetime import date, datetime
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

import oracledb

# CLOB/BLOB'ları doğrudan str/bytes olarak getir
oracledb.defaults.fetch_lobs = False

MAX_QUERY_ROWS = 1000   # serbest sorgularda grid'e yüklenecek en fazla satır
TABLE_PREVIEW_ROWS = 100
HISTORY_FILE = os.path.join(os.path.expanduser("~"), ".oracle_client_history.json")
PROFILES_FILE = os.path.join(os.path.expanduser("~"), ".oracle_client_profiles.json")
HISTORY_LIMIT = 300

SQL_KEYWORDS = """
SELECT FROM WHERE AND OR NOT IN EXISTS BETWEEN LIKE IS NULL ORDER BY GROUP
HAVING DISTINCT UNION ALL MINUS INTERSECT INSERT INTO VALUES UPDATE SET DELETE
MERGE CREATE ALTER DROP TABLE VIEW INDEX SEQUENCE TRIGGER PROCEDURE FUNCTION
PACKAGE BEGIN END DECLARE AS ON JOIN INNER LEFT RIGHT FULL OUTER CROSS USING
CASE WHEN THEN ELSE ASC DESC ROWNUM ROWID DUAL COMMIT ROLLBACK SAVEPOINT GRANT
REVOKE TRUNCATE WITH CONNECT PRIOR START LEVEL FETCH FIRST NEXT ROWS ONLY
OFFSET PARTITION OVER VARCHAR2 NUMBER DATE TIMESTAMP CHAR CLOB BLOB INTEGER
PRIMARY KEY FOREIGN REFERENCES UNIQUE CHECK DEFAULT CONSTRAINT CASCADE
NVL NVL2 DECODE COALESCE TO_CHAR TO_DATE TO_NUMBER SUBSTR INSTR TRIM LTRIM
RTRIM UPPER LOWER INITCAP LENGTH REPLACE LPAD RPAD ROUND TRUNC MOD ABS CEIL
FLOOR POWER SQRT SIGN COUNT SUM AVG MIN MAX LISTAGG ROW_NUMBER RANK DENSE_RANK
LAG LEAD SYSDATE SYSTIMESTAMP CURRENT_DATE ADD_MONTHS MONTHS_BETWEEN LAST_DAY
NEXT_DAY EXTRACT GREATEST LEAST REGEXP_LIKE REGEXP_REPLACE REGEXP_SUBSTR
""".split()

KEYWORD_RE = re.compile(r"\b(" + "|".join(SQL_KEYWORDS) + r")\b", re.IGNORECASE)
STRING_RE = re.compile(r"'[^']*'")
COMMENT_RE = re.compile(r"--[^\n]*|/\*.*?\*/", re.DOTALL)
NUMBER_RE = re.compile(r"\b\d+(\.\d+)?\b")
WORD_RE = re.compile(r"[A-Za-z0-9_$#]+$")

EDITOR_FONT = ("Menlo", 13)

OBJ_FILTER_PH = "Nesne adı filtrele..."
DDL_SEARCH_PH = "DDL / kaynak içinde ara..."


class QueryTab:
    """Bir sorgu sekmesi: SQL editörü + sonuç grid'i + kendi sonucu/dosyası."""

    def __init__(self, app, notebook, title):
        self.app = app
        self.title = title
        self.script_path = None
        self.last_columns = None
        self.last_rows = None
        self.last_sql = ""
        self.last_truncated = False
        self.last_elapsed = None

        self.frame = ttk.Frame(notebook)
        paned = ttk.PanedWindow(self.frame, orient=tk.VERTICAL)
        paned.pack(fill=tk.BOTH, expand=True)

        editor_body = ttk.Frame(paned)
        paned.add(editor_body, weight=1)
        self.sql_text = tk.Text(editor_body, height=8, wrap=tk.NONE,
                                font=EDITOR_FONT, undo=True,
                                background="#ffffff", foreground="#1f1f1f",
                                insertbackground="#1f1f1f")
        sql_scroll = ttk.Scrollbar(editor_body, orient=tk.VERTICAL,
                                   command=self.sql_text.yview)
        self.sql_text.configure(yscrollcommand=sql_scroll.set)
        sql_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.sql_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self.sql_text.tag_configure("kw", foreground="#0033b3",
                                    font=EDITOR_FONT + ("bold",))
        self.sql_text.tag_configure("num", foreground="#a34a00")
        self.sql_text.tag_configure("str", foreground="#067d17")
        self.sql_text.tag_configure("com", foreground="#8c8c8c",
                                    font=EDITOR_FONT + ("italic",))

        self.sql_text.bind("<Command-Return>", lambda e: (app.run_query(), "break")[1])
        self.sql_text.bind("<F5>", lambda e: (app.run_query(), "break")[1])
        self.sql_text.bind("<Command-s>", lambda e: (app.save_script(), "break")[1])
        self.sql_text.bind("<Command-o>", lambda e: (app.open_script(), "break")[1])
        self.sql_text.bind("<Control-space>", app._force_autocomplete)
        self.sql_text.bind("<KeyPress>", app._on_editor_keypress)
        self.sql_text.bind("<KeyRelease>", app._on_editor_keyrelease)
        self.sql_text.bind("<Button-1>", lambda e: app._hide_autocomplete())

        grid_frame = ttk.Frame(paned)
        paned.add(grid_frame, weight=3)
        self.grid = ttk.Treeview(grid_frame, show="headings", selectmode="extended")
        grid_y = ttk.Scrollbar(grid_frame, orient=tk.VERTICAL, command=self.grid.yview)
        grid_x = ttk.Scrollbar(grid_frame, orient=tk.HORIZONTAL, command=self.grid.xview)
        self.grid.configure(yscrollcommand=grid_y.set, xscrollcommand=grid_x.set)
        grid_y.pack(side=tk.RIGHT, fill=tk.Y)
        grid_x.pack(side=tk.BOTTOM, fill=tk.X)
        self.grid.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        notebook.add(self.frame, text=title)

    def set_sql(self, sql):
        self.sql_text.delete("1.0", tk.END)
        self.sql_text.insert("1.0", sql)
        self.app._highlight(self.sql_text)

    def get_sql(self):
        return self.sql_text.get("1.0", tk.END).strip().rstrip(";")

    def clear_grid(self):
        self.grid.delete(*self.grid.get_children())
        self.grid.configure(columns=())
        self.last_columns = None
        self.last_rows = None

    def show_rows(self, columns, rows):
        self.clear_grid()
        self.grid.configure(columns=columns)
        for col in columns:
            self.grid.heading(col, text=col)
            width = max(80, min(300, len(col) * 10 + 20))
            self.grid.column(col, width=width, stretch=False)
        for row in rows:
            display = ["(null)" if v is None else str(v) for v in row]
            self.grid.insert("", tk.END, values=display)
        self.last_columns = columns
        self.last_rows = rows


class OracleClientApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("Oracle Client")
        self.root.geometry("1300x820")

        self.conn = None
        self.result_queue = queue.Queue()
        self.busy = False

        self.schemas = []               # tüm şema adları
        self.schema_objects = {}        # şema -> [(tip, ad)] (lazy yüklenir)
        self.open_schemas = set()       # ağaçta açık şemalar
        self.selected_schemas = set()   # çok-seçimli şema filtresi (boş = tümü)
        self.pending_loads = []         # yenileme sonrası sırayla yüklenecek şemalar
        self.known_objects = {}         # NESNE_ADI -> şema (intellisense için)
        self.columns_cache = {}         # NESNE_ADI -> [kolonlar]
        self.completions = set(SQL_KEYWORDS)

        self.tabs = []                  # QueryTab listesi
        self.tab_counter = 0
        self._hl_job = None             # renklendirme debounce
        self.profiles = self._load_json(PROFILES_FILE, {})

        self._build_connection_bar()
        self._build_main_area()
        self._build_status_bar()
        self._build_autocomplete()

        self.new_tab()                  # ilk boş sekme

        self.root.bind("<Command-t>", lambda e: self.new_tab())
        self.root.bind("<Command-w>", lambda e: self.close_current_tab())

        self.root.after(100, self._poll_queue)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        # macOS: pencereyi öne getir ve odakla (arka planda odaksız açılmasın)
        self.root.lift()
        self.root.attributes("-topmost", True)
        self.root.after(300, lambda: self.root.attributes("-topmost", False))
        self.root.focus_force()

    # ---------------------------------------------------- Sekme yönetimi

    @property
    def sql_text(self):
        return self.current_tab().sql_text

    def current_tab(self) -> QueryTab:
        sel = self.query_nb.select()
        for tab in self.tabs:
            if str(tab.frame) == sel:
                return tab
        return self.tabs[0]

    def new_tab(self, sql="", title=None):
        self.tab_counter += 1
        title = title or f"Sorgu {self.tab_counter}"
        tab = QueryTab(self, self.query_nb, title)
        self.tabs.append(tab)
        if sql:
            tab.set_sql(sql)
        self.query_nb.select(tab.frame)
        tab.sql_text.focus_set()
        return tab

    def close_current_tab(self):
        tab = self.current_tab()
        if len(self.tabs) == 1:
            # son sekme kapanmaz, içeriği temizlenir
            tab.set_sql("")
            tab.clear_grid()
            tab.script_path = None
            self.query_nb.tab(tab.frame, text=tab.title)
            self._update_export_btn()
            return
        self.query_nb.forget(tab.frame)
        self.tabs.remove(tab)
        self._update_export_btn()

    def _rename_tab(self, tab, title):
        tab.title = title
        self.query_nb.tab(tab.frame, text=title)

    def _update_export_btn(self, *_):
        tab = self.current_tab() if self.tabs else None
        state = tk.NORMAL if tab and tab.last_columns else tk.DISABLED
        self.export_btn.configure(state=state)
        self.report_btn.configure(state=state)

    # ------------------------------------------------------------------ UI

    def _build_connection_bar(self):
        wrap = ttk.Frame(self.root, padding=(8, 6, 8, 2))
        wrap.pack(fill=tk.X)

        # 1. satır: bağlantı profilleri
        prow = ttk.Frame(wrap)
        prow.pack(fill=tk.X, pady=(0, 4))
        ttk.Label(prow, text="Profil:").pack(side=tk.LEFT)
        self.profile_var = tk.StringVar()
        self.profile_combo = ttk.Combobox(prow, textvariable=self.profile_var,
                                          state="readonly", width=22,
                                          values=sorted(self.profiles))
        self.profile_combo.pack(side=tk.LEFT, padx=(4, 6))
        self.profile_combo.bind("<<ComboboxSelected>>", self._on_profile_selected)
        ttk.Button(prow, text="Profili Kaydet",
                   command=self._save_profile).pack(side=tk.LEFT, padx=2)
        ttk.Button(prow, text="Profili Sil",
                   command=self._delete_profile).pack(side=tk.LEFT, padx=2)
        self.save_pw_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(prow, text="Şifreyi de kaydet",
                        variable=self.save_pw_var).pack(side=tk.LEFT, padx=8)

        # 2. satır: bağlantı alanları
        bar = ttk.Frame(wrap)
        bar.pack(fill=tk.X)

        ttk.Label(bar, text="Sunucu:").pack(side=tk.LEFT)
        self.host_var = tk.StringVar(value="localhost")
        ttk.Entry(bar, textvariable=self.host_var, width=16).pack(side=tk.LEFT, padx=(2, 8))

        ttk.Label(bar, text="Port:").pack(side=tk.LEFT)
        self.port_var = tk.StringVar(value="1521")
        ttk.Entry(bar, textvariable=self.port_var, width=6).pack(side=tk.LEFT, padx=(2, 8))

        ttk.Label(bar, text="Servis/SID:").pack(side=tk.LEFT)
        self.service_var = tk.StringVar(value="ORCLPDB1")
        ttk.Entry(bar, textvariable=self.service_var, width=13).pack(side=tk.LEFT, padx=(2, 8))

        ttk.Label(bar, text="Kullanıcı:").pack(side=tk.LEFT)
        self.user_var = tk.StringVar()
        ttk.Entry(bar, textvariable=self.user_var, width=13).pack(side=tk.LEFT, padx=(2, 8))

        ttk.Label(bar, text="Şifre:").pack(side=tk.LEFT)
        self.pass_var = tk.StringVar()
        pw = ttk.Entry(bar, textvariable=self.pass_var, width=13, show="•")
        pw.pack(side=tk.LEFT, padx=(2, 8))
        pw.bind("<Return>", lambda e: self.connect())

        self.connect_btn = ttk.Button(bar, text="Bağlan", command=self.connect)
        self.connect_btn.pack(side=tk.LEFT, padx=4)
        self.disconnect_btn = ttk.Button(bar, text="Bağlantıyı Kes",
                                         command=self.disconnect, state=tk.DISABLED)
        self.disconnect_btn.pack(side=tk.LEFT)

    def _build_main_area(self):
        outer = ttk.PanedWindow(self.root, orient=tk.HORIZONTAL)
        outer.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 4))

        # ---- Sol panel: Nesneler + Tarihçe sekmeleri
        left_nb = ttk.Notebook(outer)
        outer.add(left_nb, weight=1)

        left = ttk.Frame(left_nb, padding=4)
        left_nb.add(left, text="Nesneler")

        hist_tab = ttk.Frame(left_nb, padding=4)
        left_nb.add(hist_tab, text="Tarihçe")
        self._build_history_tab(hist_tab)

        # Çok-seçimli şema filtresi + yenile
        srow = ttk.Frame(left)
        srow.pack(fill=tk.X, pady=(0, 4))
        self.schema_btn = ttk.Button(srow, text="Şemalar ▾",
                                     command=self._open_schema_picker,
                                     state=tk.DISABLED)
        self.schema_btn.pack(side=tk.LEFT)
        self.refresh_btn = ttk.Button(srow, text="⟳ Yenile",
                                      command=self.refresh_objects,
                                      state=tk.DISABLED)
        self.refresh_btn.pack(side=tk.RIGHT)

        # Nesne adı filtresi (ağaç)
        self.filter_var = tk.StringVar()
        self.filter_var.trace_add("write", lambda *a: self._apply_filter())
        filter_entry = ttk.Entry(left, textvariable=self.filter_var)
        filter_entry.pack(fill=tk.X, pady=(0, 4))
        self._add_placeholder(filter_entry, OBJ_FILTER_PH)

        # DDL / kaynak serbest metin arama
        ddl_row = ttk.Frame(left)
        ddl_row.pack(fill=tk.X, pady=(0, 4))
        self.ddl_search_var = tk.StringVar()
        ddl_entry = ttk.Entry(ddl_row, textvariable=self.ddl_search_var)
        ddl_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 4))
        self._add_placeholder(ddl_entry, DDL_SEARCH_PH)
        ddl_entry.bind("<Return>", lambda e: self.search_ddl())
        self.ddl_btn = ttk.Button(ddl_row, text="🔍 DDL Ara",
                                  command=self.search_ddl, state=tk.DISABLED)
        self.ddl_btn.pack(side=tk.RIGHT)

        tree_frame = ttk.Frame(left)
        tree_frame.pack(fill=tk.BOTH, expand=True)
        self.obj_tree = ttk.Treeview(tree_frame, show="tree", selectmode="browse")
        obj_scroll = ttk.Scrollbar(tree_frame, orient=tk.VERTICAL,
                                   command=self.obj_tree.yview)
        self.obj_tree.configure(yscrollcommand=obj_scroll.set)
        obj_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.obj_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.obj_tree.bind("<Double-1>", self._on_object_open)
        self.obj_tree.bind("<Return>", self._on_object_open)
        self.obj_tree.bind("<<TreeviewOpen>>", self._on_tree_expand)
        self.obj_tree.bind("<<TreeviewClose>>", self._on_tree_collapse)

        # ---- Sağ panel: araç çubuğu + sorgu sekmeleri
        right = ttk.Frame(outer)
        outer.add(right, weight=4)

        toolbar = ttk.Frame(right)
        toolbar.pack(fill=tk.X, pady=(0, 2))
        self.run_btn = ttk.Button(toolbar, text="▶ Çalıştır (⌘↩)",
                                  command=self.run_query, state=tk.DISABLED)
        self.run_btn.pack(side=tk.LEFT)
        ttk.Separator(toolbar, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=6)
        ttk.Button(toolbar, text="＋ Yeni Sekme (⌘T)",
                   command=lambda: self.new_tab()).pack(side=tk.LEFT, padx=2)
        ttk.Button(toolbar, text="✕ Sekmeyi Kapat (⌘W)",
                   command=self.close_current_tab).pack(side=tk.LEFT, padx=2)
        ttk.Separator(toolbar, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=6)
        ttk.Button(toolbar, text="📂 Aç",
                   command=self.open_script).pack(side=tk.LEFT, padx=2)
        ttk.Button(toolbar, text="💾 Kaydet (⌘S)",
                   command=self.save_script).pack(side=tk.LEFT, padx=2)
        ttk.Button(toolbar, text="Farklı Kaydet",
                   command=lambda: self.save_script(save_as=True)).pack(side=tk.LEFT, padx=2)
        ttk.Separator(toolbar, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=6)
        self.export_btn = ttk.Button(toolbar, text="⬇ CSV'ye Aktar",
                                     command=self.export_csv, state=tk.DISABLED)
        self.export_btn.pack(side=tk.LEFT, padx=2)
        self.report_btn = ttk.Button(toolbar, text="📊 Rapor Oluştur",
                                     command=self.generate_report, state=tk.DISABLED)
        self.report_btn.pack(side=tk.LEFT, padx=2)

        self.query_nb = ttk.Notebook(right)
        self.query_nb.pack(fill=tk.BOTH, expand=True)
        self.query_nb.bind("<<NotebookTabChanged>>", self._update_export_btn)

    def _build_history_tab(self, parent):
        top = ttk.Frame(parent)
        top.pack(fill=tk.X, pady=(0, 4))
        ttk.Button(top, text="Temizle", command=self._clear_history).pack(side=tk.RIGHT)
        ttk.Label(top, text="Çift tık: yeni sekmede aç").pack(side=tk.LEFT)

        frame = ttk.Frame(parent)
        frame.pack(fill=tk.BOTH, expand=True)
        self.hist_tree = ttk.Treeview(frame, columns=("time", "sql"),
                                      show="headings", selectmode="browse")
        self.hist_tree.heading("time", text="Zaman")
        self.hist_tree.heading("sql", text="Sorgu")
        self.hist_tree.column("time", width=110, stretch=False)
        self.hist_tree.column("sql", width=220)
        hist_scroll = ttk.Scrollbar(frame, orient=tk.VERTICAL,
                                    command=self.hist_tree.yview)
        self.hist_tree.configure(yscrollcommand=hist_scroll.set)
        hist_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.hist_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.hist_tree.bind("<Double-1>", self._on_history_open)
        self.hist_tree.bind("<Return>", self._on_history_open)

        self.history = self._load_json(HISTORY_FILE, [])[:HISTORY_LIMIT]
        self._render_history()

    def _build_status_bar(self):
        self.status_var = tk.StringVar(value="Bağlı değil")
        ttk.Label(self.root, textvariable=self.status_var, anchor=tk.W,
                  padding=(8, 3)).pack(fill=tk.X, side=tk.BOTTOM)

    def _build_autocomplete(self):
        self.ac_win = tk.Toplevel(self.root)
        self.ac_win.withdraw()
        self.ac_win.overrideredirect(True)
        self.ac_list = tk.Listbox(self.ac_win, font=("Menlo", 12), height=8,
                                  activestyle="dotbox",
                                  background="#fffbe8", foreground="#1f1f1f")
        self.ac_list.pack(fill=tk.BOTH, expand=True)
        self.ac_list.bind("<Double-1>", lambda e: self._accept_autocomplete())

    @staticmethod
    def _add_placeholder(entry: ttk.Entry, text: str):
        entry.insert(0, text)
        entry.configure(foreground="grey")

        def on_focus_in(_):
            if entry.get() == text and str(entry.cget("foreground")) == "grey":
                entry.delete(0, tk.END)
                entry.configure(foreground="black")

        def on_focus_out(_):
            if not entry.get():
                entry.insert(0, text)
                entry.configure(foreground="grey")

        entry.bind("<FocusIn>", on_focus_in)
        entry.bind("<FocusOut>", on_focus_out)

    # ----------------------------------------------------- JSON yardımcıları

    @staticmethod
    def _load_json(path, default):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, type(default)):
                return data
        except (OSError, ValueError):
            pass
        return default

    @staticmethod
    def _save_json(path, data):
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=1)
        except OSError:
            pass

    # ------------------------------------------------------------ Profiller

    def _save_profile(self):
        host = self.host_var.get().strip()
        user = self.user_var.get().strip()
        if not host or not user:
            messagebox.showwarning("Eksik bilgi", "Önce bağlantı alanlarını doldurun.")
            return
        default_name = f"{user}@{host}/{self.service_var.get().strip()}"
        name = simpledialog.askstring("Profili Kaydet", "Profil adı:",
                                      initialvalue=self.profile_var.get() or default_name,
                                      parent=self.root)
        if not name:
            return
        profile = {
            "host": host,
            "port": self.port_var.get().strip(),
            "service": self.service_var.get().strip(),
            "user": user,
        }
        if self.save_pw_var.get() and self.pass_var.get():
            profile["password"] = base64.b64encode(
                self.pass_var.get().encode("utf-8")).decode("ascii")
        self.profiles[name] = profile
        self._save_json(PROFILES_FILE, self.profiles)
        self.profile_combo.configure(values=sorted(self.profiles))
        self.profile_var.set(name)
        self._set_status(f"Profil kaydedildi: {name}")

    def _delete_profile(self):
        name = self.profile_var.get()
        if not name or name not in self.profiles:
            return
        if messagebox.askyesno("Profili Sil", f"'{name}' profili silinsin mi?"):
            del self.profiles[name]
            self._save_json(PROFILES_FILE, self.profiles)
            self.profile_combo.configure(values=sorted(self.profiles))
            self.profile_var.set("")
            self._set_status(f"Profil silindi: {name}")

    def _on_profile_selected(self, _event):
        profile = self.profiles.get(self.profile_var.get())
        if not profile:
            return
        self.host_var.set(profile.get("host", ""))
        self.port_var.set(profile.get("port", "1521"))
        self.service_var.set(profile.get("service", ""))
        self.user_var.set(profile.get("user", ""))
        pw = profile.get("password")
        if pw:
            try:
                self.pass_var.set(base64.b64decode(pw).decode("utf-8"))
            except Exception:
                self.pass_var.set("")
        else:
            self.pass_var.set("")

    # ---------------------------------------------------------- Bağlantı

    def connect(self):
        if self.conn is not None:
            return
        host = self.host_var.get().strip()
        port = self.port_var.get().strip()
        service = self.service_var.get().strip()
        user = self.user_var.get().strip()
        password = self.pass_var.get()
        if not all([host, port, service, user, password]):
            messagebox.showwarning("Eksik bilgi", "Tüm bağlantı alanlarını doldurun.")
            return

        dsn = f"{host}:{port}/{service}"
        self._set_status(f"{dsn} adresine bağlanılıyor...")
        self.connect_btn.configure(state=tk.DISABLED)

        def work():
            try:
                conn = oracledb.connect(user=user, password=password, dsn=dsn)
                self.result_queue.put(("connected", conn))
            except Exception as exc:
                self.result_queue.put(("connect_error", str(exc)))

        threading.Thread(target=work, daemon=True).start()

    def disconnect(self):
        if self.conn is not None:
            try:
                self.conn.close()
            except Exception:
                pass
            self.conn = None
        self.connect_btn.configure(state=tk.NORMAL)
        self.disconnect_btn.configure(state=tk.DISABLED)
        self.run_btn.configure(state=tk.DISABLED)
        self.refresh_btn.configure(state=tk.DISABLED)
        self.schema_btn.configure(state=tk.DISABLED)
        self.ddl_btn.configure(state=tk.DISABLED)
        self.schemas = []
        self.schema_objects = {}
        self.open_schemas = set()
        self.selected_schemas = set()
        self._update_schema_btn()
        self.pending_loads = []
        self.known_objects = {}
        self.columns_cache = {}
        self.completions = set(SQL_KEYWORDS)
        self.obj_tree.delete(*self.obj_tree.get_children())
        self._set_status("Bağlı değil")

    # ------------------------------------------------------ Nesne ağacı
    #  Yapı: şema -> Tablolar/Görünümler -> nesne (lazy yükleme)

    def load_schemas(self):
        def work(conn):
            cur = conn.cursor()
            cur.execute("SELECT username FROM all_users ORDER BY username")
            schemas = [r[0] for r in cur.fetchall()]
            cur.close()
            return ("schemas", schemas)
        self._run_in_thread(work)

    def refresh_objects(self):
        """Şema listesini ve açık şemaların nesnelerini yeniden yükle."""
        if self.conn is None or self.busy:
            return
        self.schema_objects = {}
        self.known_objects = {}
        self.columns_cache = {}
        self.completions = set(SQL_KEYWORDS)
        self.pending_loads = sorted(self.open_schemas)
        self._set_status("Nesneler yenileniyor...")
        self.load_schemas()

    # ------------------------------------------------ Çok-seçimli şema filtresi

    def _update_schema_btn(self):
        n = len(self.selected_schemas)
        self.schema_btn.configure(text=f"Şemalar ({n}) ▾" if n else "Şemalar ▾")

    def _open_schema_picker(self):
        if not self.schemas:
            return
        win = tk.Toplevel(self.root)
        win.title("Şema seç")
        win.geometry("320x460")
        win.transient(self.root)

        fv = tk.StringVar()
        fe = ttk.Entry(win, textvariable=fv)
        fe.pack(fill=tk.X, padx=8, pady=(8, 4))
        self._add_placeholder(fe, "Şema ara...")

        body = ttk.Frame(win)
        body.pack(fill=tk.BOTH, expand=True, padx=(8, 0))
        canvas = tk.Canvas(body, highlightthickness=0, width=1)
        sb = ttk.Scrollbar(body, orient=tk.VERTICAL, command=canvas.yview)
        inner = ttk.Frame(canvas)
        inner.bind("<Configure>",
                   lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.configure(yscrollcommand=sb.set)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb.pack(side=tk.RIGHT, fill=tk.Y)

        vars_map = {}

        def rebuild(*_):
            for w in inner.winfo_children():
                w.destroy()
            term = fv.get().strip()
            if term == "Şema ara...":
                term = ""
            term = term.upper()
            for s in self.schemas:
                if term and term not in s.upper():
                    continue
                v = vars_map.get(s)
                if v is None:
                    v = tk.BooleanVar(value=(s in self.selected_schemas))
                    vars_map[s] = v
                ttk.Checkbutton(inner, text=s, variable=v).pack(anchor="w", pady=1)

        fv.trace_add("write", rebuild)
        rebuild()

        btns = ttk.Frame(win)
        btns.pack(fill=tk.X, padx=8, pady=8)

        def set_all(val):
            for s in self.schemas:
                vars_map.setdefault(s, tk.BooleanVar()).set(val)
            rebuild()

        ttk.Button(btns, text="Tümü", command=lambda: set_all(True)).pack(side=tk.LEFT)
        ttk.Button(btns, text="Hiçbiri", command=lambda: set_all(False)).pack(side=tk.LEFT, padx=4)

        def apply():
            self.selected_schemas = {s for s, v in vars_map.items() if v.get()}
            self._update_schema_btn()
            self._render_tree()
            # yeni seçilen ve henüz yüklenmemiş şemaların nesnelerini çek
            to_load = [s for s in sorted(self.selected_schemas)
                       if self.schema_objects.get(s) is None
                       and s not in self.pending_loads]
            for s in to_load:
                self.open_schemas.add(s)
                self.pending_loads.append(s)
            if self.pending_loads and not self.busy:
                self._next_pending_load()
            win.destroy()

        ttk.Button(btns, text="Uygula", command=apply).pack(side=tk.RIGHT)
        ttk.Button(btns, text="İptal", command=win.destroy).pack(side=tk.RIGHT, padx=4)

        win.update_idletasks()
        win.lift()
        fe.focus_set()

    # ------------------------------------------------ DDL / kaynak arama

    def search_ddl(self):
        if self.conn is None:
            return
        if self.busy:
            self._set_status("Başka bir işlem çalışıyor, bitmesini bekleyin...")
            return
        term = self.ddl_search_var.get().strip()
        if term in ("", DDL_SEARCH_PH):
            return
        scope = (sorted(self.selected_schemas) if self.selected_schemas
                 else list(self.schemas))
        if not scope:
            return
        if not self.selected_schemas and len(scope) > 5:
            if not messagebox.askyesno(
                    "Tüm şemalarda ara",
                    f"Şema seçilmedi. Arama {len(scope)} şemanın tümünde "
                    "yapılacak; büyük veritabanlarında yavaş olabilir.\n\n"
                    "Devam edilsin mi?"):
                return

        self._set_status(f"DDL/kaynak aranıyor: '{term}' — {len(scope)} şema...")
        self.ddl_btn.configure(state=tk.DISABLED)

        def work(conn):
            cur = conn.cursor()
            like = "%" + term.upper() + "%"
            results = []
            # Oracle IN listesi 1000 ifade ile sınırlı — parça parça sorgula
            for i in range(0, len(scope), 1000):
                chunk = scope[i:i + 1000]
                binds = {f"s{j}": s for j, s in enumerate(chunk)}
                inlist = ",".join(":" + k for k in binds)
                # PL/SQL kaynak nesneleri (procedure/function/package/trigger/type...)
                cur.execute(
                    f"SELECT owner, type, name, line, text FROM all_source "
                    f"WHERE owner IN ({inlist}) AND UPPER(text) LIKE :q "
                    f"ORDER BY owner, name, line",
                    {**binds, "q": like})
                for owner, typ, name, line, text in cur.fetchall():
                    results.append((owner, typ, name, line,
                                    (text or "").rstrip("\n")))
                # View tanımları (text_vc, 12c+); yoksa sessizce atla
                try:
                    cur.execute(
                        f"SELECT owner, view_name, text_vc FROM all_views "
                        f"WHERE owner IN ({inlist}) AND UPPER(text_vc) LIKE :q "
                        f"ORDER BY owner, view_name",
                        {**binds, "q": like})
                    for owner, name, text in cur.fetchall():
                        results.append((owner, "VIEW", name, 1,
                                        (text or "").strip()[:4000]))
                except Exception:
                    pass
            cur.close()
            return ("ddl_results", term, results)
        self._run_in_thread(work)

    def _show_ddl_results(self, term, results):
        tab = self.new_tab(title=f"DDL: {term[:20]}")
        tab.set_sql(
            f"-- DDL/kaynak araması: '{term}' — {len(results)} eşleşme\n"
            "-- Bir satıra çift tıklayın: nesnenin tam kaynağı yeni sekmede açılır")
        cols = ["ŞEMA", "TİP", "NESNE", "SATIR", "METİN"]
        tab.show_rows(cols, list(results))
        tab.last_sql = f"-- DDL/kaynak araması: '{term}'"
        tab.last_truncated = False
        tab.grid.bind("<Double-1>", lambda e: self._on_ddl_result_open(tab))
        tab.grid.bind("<Return>", lambda e: self._on_ddl_result_open(tab))

    def _on_ddl_result_open(self, tab):
        sel = tab.grid.focus()
        if not sel:
            return
        vals = tab.grid.item(sel, "values")
        if not vals or len(vals) < 3:
            return
        self._open_object_ddl(vals[0], vals[1], vals[2])

    def _open_object_ddl(self, owner, typ, name):
        """DDL arama sonucundaki bir nesnenin tam kaynağını yeni sekmede aç."""
        if self.conn is None or self.busy:
            return
        self._set_status(f"{owner}.{name} kaynağı getiriliyor...")

        def work(conn):
            cur = conn.cursor()
            if typ == "VIEW":
                cur.execute("SELECT text FROM all_views "
                            "WHERE owner = :o AND view_name = :n", o=owner, n=name)
                row = cur.fetchone()
                body = row[0] if row else ""
                ddl = (f'CREATE OR REPLACE VIEW "{owner}"."{name}" AS\n{body}')
            else:
                cur.execute("SELECT text FROM all_source "
                            "WHERE owner = :o AND name = :n AND type = :t "
                            "ORDER BY line", o=owner, n=name, t=typ)
                lines = [r[0] for r in cur.fetchall()]
                ddl = "CREATE OR REPLACE " + "".join(lines) if lines else ""
            cur.close()
            return ("object_ddl", owner, name, ddl)
        self._run_in_thread(work)

    def _load_schema_objects(self, schema):
        self._set_status(f"{schema} şemasındaki nesneler yükleniyor...")

        def work(conn):
            cur = conn.cursor()
            cur.execute(
                "SELECT object_name, object_type FROM all_objects "
                "WHERE owner = :o AND object_type IN ('TABLE','VIEW') "
                "ORDER BY object_type, object_name",
                o=schema)
            objs = [(otype, name) for name, otype in cur.fetchall()]
            cur.close()
            return ("objects", schema, objs)
        self._run_in_thread(work)

    def _render_tree(self):
        self.obj_tree.delete(*self.obj_tree.get_children())
        filt = self.filter_var.get().strip()
        if filt == OBJ_FILTER_PH:
            filt = ""
        filt = filt.upper()

        for schema in self.schemas:
            if self.selected_schemas and schema not in self.selected_schemas:
                continue
            objs = self.schema_objects.get(schema)
            if objs is None:
                # henüz yüklenmemiş şema — filtre varsa şema adına uygula
                if filt and filt not in schema:
                    continue
                node = self.obj_tree.insert("", tk.END, text=schema,
                                            values=("SCHEMA", schema, ""))
                self.obj_tree.insert(node, tk.END, text="…",
                                     values=("DUMMY", schema, ""))
            else:
                shown = ([o for o in objs if filt in o[1].upper()]
                         if filt else objs)
                if filt and not shown and filt not in schema:
                    continue
                is_open = schema in self.open_schemas or bool(filt and shown)
                node = self.obj_tree.insert("", tk.END, text=schema, open=is_open,
                                            values=("SCHEMA", schema, ""))
                tables = [n for t, n in shown if t == "TABLE"]
                views = [n for t, n in shown if t == "VIEW"]
                tnode = self.obj_tree.insert(node, tk.END,
                                             text=f"Tablolar ({len(tables)})",
                                             open=is_open,
                                             values=("GROUP", schema, "TABLE"))
                for name in tables:
                    self.obj_tree.insert(tnode, tk.END, text=name,
                                         values=("TABLE", schema, name))
                vnode = self.obj_tree.insert(node, tk.END,
                                             text=f"Görünümler ({len(views)})",
                                             open=is_open,
                                             values=("GROUP", schema, "VIEW"))
                for name in views:
                    self.obj_tree.insert(vnode, tk.END, text=name,
                                         values=("VIEW", schema, name))

    def _on_tree_expand(self, _event):
        item = self.obj_tree.focus()
        if not item:
            return
        values = self.obj_tree.item(item, "values")
        if not values:
            return
        kind, schema = values[0], values[1]
        if kind == "SCHEMA":
            self.open_schemas.add(schema)
            if self.schema_objects.get(schema) is None and not self.busy:
                self._load_schema_objects(schema)

    def _on_tree_collapse(self, _event):
        item = self.obj_tree.focus()
        if not item:
            return
        values = self.obj_tree.item(item, "values")
        if values and values[0] == "SCHEMA":
            self.open_schemas.discard(values[1])

    def _apply_filter(self):
        if not hasattr(self, "obj_tree"):   # UI henüz kurulurken tetiklenmesin
            return
        self._render_tree()

    def _on_object_open(self, _event):
        item = self.obj_tree.focus()
        if not item:
            return
        values = self.obj_tree.item(item, "values")
        if not values or values[0] not in ("TABLE", "VIEW"):
            return
        _kind, schema, name = values
        sql = f'SELECT * FROM "{schema}"."{name}" WHERE ROWNUM <= {TABLE_PREVIEW_ROWS}'
        self.new_tab(sql=sql, title=name)
        self.run_query()
        self._fetch_columns(schema, name)

    # ------------------------------------------------------------- Sorgu

    def run_query(self):
        if self.conn is None:
            return
        if self.busy:
            self._set_status("Başka bir sorgu çalışıyor, bitmesini bekleyin...")
            return
        self._hide_autocomplete()
        tab = self.current_tab()
        sql = tab.get_sql()
        if not sql:
            return
        self._add_history(sql)
        tab.last_sql = sql
        self._set_status(f"[{tab.title}] Sorgu çalışıyor...")
        self.run_btn.configure(state=tk.DISABLED)

        def work(conn):
            start = time.perf_counter()
            cur = conn.cursor()
            try:
                cur.execute(sql)
                if cur.description is None:
                    # SELECT dışı ifade (INSERT/UPDATE/DDL...)
                    affected = cur.rowcount
                    conn.commit()
                    elapsed = time.perf_counter() - start
                    return ("dml_done", tab, affected, elapsed)
                columns = [d[0] for d in cur.description]
                rows = cur.fetchmany(MAX_QUERY_ROWS)
                truncated = cur.fetchone() is not None
                elapsed = time.perf_counter() - start
                return ("rows", tab, columns, rows, truncated, elapsed)
            finally:
                cur.close()
        self._run_in_thread(work)

    def export_csv(self):
        tab = self.current_tab()
        if not tab.last_columns:
            return
        path = filedialog.asksaveasfilename(
            title="CSV olarak kaydet", defaultextension=".csv",
            filetypes=[("CSV dosyası", "*.csv"), ("Tüm dosyalar", "*.*")],
            initialfile="sorgu_sonucu.csv", parent=self.root)
        if not path:
            return
        try:
            with open(path, "w", newline="", encoding="utf-8-sig") as f:
                writer = csv.writer(f, delimiter=";")
                writer.writerow(tab.last_columns)
                for row in tab.last_rows:
                    writer.writerow(["" if v is None else v for v in row])
            self._set_status(f"{len(tab.last_rows)} satır CSV'ye aktarıldı: {path}")
        except OSError as exc:
            messagebox.showerror("CSV hatası", str(exc))

    # -------------------------------------------------------- HTML rapor

    def generate_report(self):
        tab = self.current_tab()
        if not tab.last_columns:
            return
        default = re.sub(r"[^0-9A-Za-z_-]+", "_", tab.title).strip("_") or "rapor"
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = filedialog.asksaveasfilename(
            title="Raporu kaydet", defaultextension=".html",
            filetypes=[("HTML rapor", "*.html"), ("Tüm dosyalar", "*.*")],
            initialfile=f"{default}_{stamp}.html", parent=self.root)
        if not path:
            return
        try:
            html_text = self._build_report_html(tab)
            with open(path, "w", encoding="utf-8") as f:
                f.write(html_text)
        except OSError as exc:
            messagebox.showerror("Rapor hatası", str(exc))
            return
        self._set_status(f"Rapor oluşturuldu: {path}")
        try:
            webbrowser.open(f"file://{path}")
        except Exception:
            pass

    @staticmethod
    def _is_number(v):
        return isinstance(v, (int, float, decimal.Decimal)) and not isinstance(v, bool)

    def _column_stats(self, columns, rows):
        """Her kolon için tip tahmini ve temel istatistikler."""
        stats = []
        n = len(rows)
        for i, col in enumerate(columns):
            values = [r[i] for r in rows]
            non_null = [v for v in values if v is not None]
            nulls = n - len(non_null)
            try:
                distinct = len({v for v in non_null})
            except TypeError:
                distinct = len({str(v) for v in non_null})

            info = {"name": col, "nulls": nulls, "distinct": distinct,
                    "type": "metin"}
            if non_null and all(self._is_number(v) for v in non_null):
                nums = [float(v) for v in non_null]
                total = sum(nums)
                info.update(type="sayı", min=min(nums), max=max(nums),
                            avg=total / len(nums), sum=total)
            elif non_null and all(isinstance(v, (date, datetime)) for v in non_null):
                info.update(type="tarih", min=min(non_null), max=max(non_null))
            stats.append(info)
        return stats

    def _pick_chart(self, columns, rows, stats):
        """Kategorik + sayısal kolon çifti bulup (etiket, değer) listesi üret."""
        cat_idx = num_idx = None
        for i, s in enumerate(stats):
            if s["type"] == "metin" and 1 < s["distinct"] <= 30 and cat_idx is None:
                cat_idx = i
            if s["type"] == "sayı" and num_idx is None:
                num_idx = i
        if cat_idx is None or num_idx is None:
            return None
        agg = {}
        for r in rows:
            key = r[cat_idx]
            val = r[num_idx]
            if key is None or not self._is_number(val):
                continue
            agg[str(key)] = agg.get(str(key), 0.0) + float(val)
        if not agg:
            return None
        top = sorted(agg.items(), key=lambda kv: kv[1], reverse=True)[:15]
        return {"label": columns[cat_idx], "measure": columns[num_idx], "data": top}

    @staticmethod
    def _fmt(v):
        if v is None:
            return ""
        if isinstance(v, bool):
            return str(v)
        if isinstance(v, float):
            s = f"{v:,.2f}"
        elif isinstance(v, (int, decimal.Decimal)):
            s = f"{v:,}"
        else:
            return str(v)
        # US biçimi (,/.) -> TR biçimi (./,)
        return s.replace(",", "\x1f").replace(".", ",").replace("\x1f", ".")

    def _svg_bar_chart(self, chart):
        data = chart["data"]
        if not data:
            return ""
        maxv = max(v for _, v in data) or 1
        bar_h, gap, label_w, chart_w = 26, 8, 200, 460
        height = len(data) * (bar_h + gap) + gap
        rows_svg = []
        for idx, (label, val) in enumerate(data):
            y = gap + idx * (bar_h + gap)
            w = max(1, int((val / maxv) * chart_w))
            lbl = html.escape(label[:28])
            valtxt = html.escape(self._fmt(val))
            rows_svg.append(
                f'<text x="{label_w - 8}" y="{y + bar_h * 0.68}" '
                f'text-anchor="end" class="bl">{lbl}</text>'
                f'<rect x="{label_w}" y="{y}" width="{w}" height="{bar_h}" '
                f'rx="3" class="bar"/>'
                f'<text x="{label_w + w + 6}" y="{y + bar_h * 0.68}" '
                f'class="bv">{valtxt}</text>')
        total_w = label_w + chart_w + 90
        return (
            f'<svg viewBox="0 0 {total_w} {height}" width="100%" '
            f'style="max-width:{total_w}px" xmlns="http://www.w3.org/2000/svg">'
            + "".join(rows_svg) + "</svg>")

    def _build_report_html(self, tab):
        columns = tab.last_columns
        rows = tab.last_rows
        stats = self._column_stats(columns, rows)
        chart = self._pick_chart(columns, rows, stats)
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        conn_info = ""
        if self.conn is not None:
            conn_info = (f"{html.escape(self.user_var.get())}@"
                         f"{html.escape(self.host_var.get())}:"
                         f"{html.escape(self.port_var.get())}/"
                         f"{html.escape(self.service_var.get())}")

        # Özet kartları
        cards = [("Satır", f"{len(rows):,}" + (" (ilk 1000)" if tab.last_truncated else "")),
                 ("Kolon", str(len(columns)))]
        if tab.last_elapsed is not None:
            cards.append(("Süre", f"{tab.last_elapsed:.2f} sn"))
        cards_html = "".join(
            f'<div class="card"><div class="cv">{html.escape(v)}</div>'
            f'<div class="cl">{html.escape(l)}</div></div>' for l, v in cards)

        # Kolon profili
        prof_rows = []
        for s in stats:
            extra = ""
            if s["type"] == "sayı":
                extra = (f'min {self._fmt(s["min"])} · maks {self._fmt(s["max"])} · '
                         f'ort {self._fmt(s["avg"])} · top {self._fmt(s["sum"])}')
            elif s["type"] == "tarih":
                extra = f'{self._fmt(s["min"])} → {self._fmt(s["max"])}'
            prof_rows.append(
                f'<tr><td>{html.escape(s["name"])}</td>'
                f'<td><span class="tag t-{s["type"]}">{s["type"]}</span></td>'
                f'<td class="num">{s["nulls"]:,}</td>'
                f'<td class="num">{s["distinct"]:,}</td>'
                f'<td class="mut">{html.escape(extra)}</td></tr>')
        prof_html = "".join(prof_rows)

        # Grafik bölümü
        chart_html = ""
        if chart:
            chart_html = (
                f'<h2>Grafik</h2><p class="mut">{html.escape(chart["measure"])} '
                f'toplamı, {html.escape(chart["label"])} bazında (ilk 15)</p>'
                f'<div class="chart">{self._svg_bar_chart(chart)}</div>')

        # Veri tablosu
        head = "".join(f"<th>{html.escape(c)}</th>" for c in columns)
        body_rows = []
        for r in rows:
            tds = []
            for v in r:
                cls = ' class="num"' if self._is_number(v) else ""
                cell = "<span class='null'>NULL</span>" if v is None \
                    else html.escape(self._fmt(v) if isinstance(v, (float, decimal.Decimal))
                                     else str(v))
                tds.append(f"<td{cls}>{cell}</td>")
            body_rows.append("<tr>" + "".join(tds) + "</tr>")
        body_html = "".join(body_rows)

        sql_html = html.escape(tab.last_sql or "")
        title = html.escape(tab.title)

        return f"""<!DOCTYPE html>
<html lang="tr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Rapor — {title}</title>
<style>
:root {{ --bg:#f6f7f9; --fg:#1f2328; --mut:#6b7280; --card:#fff;
  --border:#e5e7eb; --accent:#0b6bcb; --bar:#0b6bcb; }}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--bg); color:var(--fg);
  font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; }}
.wrap {{ max-width:1100px; margin:0 auto; padding:28px 20px 60px; }}
header h1 {{ margin:0 0 4px; font-size:22px; }}
header .meta {{ color:var(--mut); font-size:13px; }}
.cards {{ display:flex; gap:12px; flex-wrap:wrap; margin:20px 0; }}
.card {{ background:var(--card); border:1px solid var(--border); border-radius:10px;
  padding:14px 18px; min-width:120px; }}
.cv {{ font-size:22px; font-weight:600; }}
.cl {{ color:var(--mut); font-size:12px; text-transform:uppercase;
  letter-spacing:.04em; }}
h2 {{ font-size:16px; margin:28px 0 10px; }}
pre.sql {{ background:#0d1117; color:#e6edf3; padding:14px 16px; border-radius:10px;
  overflow:auto; font:12.5px/1.5 Menlo,Consolas,monospace; white-space:pre-wrap; }}
table {{ border-collapse:collapse; width:100%; background:var(--card);
  border:1px solid var(--border); border-radius:10px; overflow:hidden; }}
th,td {{ padding:7px 10px; text-align:left; border-bottom:1px solid var(--border);
  font-size:13px; white-space:nowrap; }}
th {{ background:#eef1f5; position:sticky; top:0; font-weight:600; }}
tr:nth-child(even) td {{ background:#fafbfc; }}
td.num {{ text-align:right; font-variant-numeric:tabular-nums; }}
.null {{ color:#c026d3; font-style:italic; font-size:12px; }}
.mut {{ color:var(--mut); }}
.tag {{ padding:2px 8px; border-radius:20px; font-size:11px; font-weight:600; }}
.t-sayı {{ background:#e0f2fe; color:#0369a1; }}
.t-metin {{ background:#f1f5f9; color:#475569; }}
.t-tarih {{ background:#dcfce7; color:#15803d; }}
.tablewrap {{ overflow:auto; max-height:70vh; border-radius:10px; }}
.chart {{ background:var(--card); border:1px solid var(--border);
  border-radius:10px; padding:16px; overflow:auto; }}
.chart text {{ font:12px -apple-system,sans-serif; fill:var(--fg); }}
.chart .bl {{ fill:#475569; }}
.chart .bv {{ fill:#475569; font-variant-numeric:tabular-nums; }}
.chart .bar {{ fill:var(--bar); }}
footer {{ margin-top:30px; color:var(--mut); font-size:12px; }}
</style></head><body><div class="wrap">
<header>
  <h1>{title}</h1>
  <div class="meta">Oracle Client raporu · {html.escape(now)}
  {(' · ' + conn_info) if conn_info else ''}</div>
</header>
<div class="cards">{cards_html}</div>
<h2>Sorgu</h2>
<pre class="sql">{sql_html}</pre>
<h2>Kolon Profili</h2>
<table><thead><tr><th>Kolon</th><th>Tip</th><th>Boş (null)</th>
<th>Tekil</th><th>İstatistik</th></tr></thead><tbody>{prof_html}</tbody></table>
{chart_html}
<h2>Veri ({len(rows):,} satır)</h2>
<div class="tablewrap"><table><thead><tr>{head}</tr></thead>
<tbody>{body_html}</tbody></table></div>
<footer>Oracle Client · github.com/kadirevciler/oracle-client-macos</footer>
</div></body></html>"""

    # ---------------------------------------------------- Betik aç/kaydet

    def open_script(self):
        path = filedialog.askopenfilename(
            title="SQL betiği aç",
            filetypes=[("SQL dosyası", "*.sql"), ("Tüm dosyalar", "*.*")],
            parent=self.root)
        if not path:
            return
        try:
            with open(path, encoding="utf-8") as f:
                content = f.read()
        except OSError as exc:
            messagebox.showerror("Dosya hatası", str(exc))
            return
        tab = self.new_tab(sql=content, title=os.path.basename(path))
        tab.script_path = path
        self._set_status(f"Betik açıldı: {path}")

    def save_script(self, save_as=False):
        tab = self.current_tab()
        if save_as or not tab.script_path:
            path = filedialog.asksaveasfilename(
                title="SQL betiğini kaydet", defaultextension=".sql",
                filetypes=[("SQL dosyası", "*.sql"), ("Tüm dosyalar", "*.*")],
                initialfile=os.path.basename(tab.script_path)
                if tab.script_path else "betik.sql",
                parent=self.root)
            if not path:
                return
            tab.script_path = path
        try:
            with open(tab.script_path, "w", encoding="utf-8") as f:
                f.write(tab.sql_text.get("1.0", "end-1c"))
        except OSError as exc:
            messagebox.showerror("Dosya hatası", str(exc))
            return
        self._rename_tab(tab, os.path.basename(tab.script_path))
        self._set_status(f"Betik kaydedildi: {tab.script_path}")

    # ----------------------------------------------- Sözdizimi renklendirme

    def _schedule_highlight(self):
        if self._hl_job is not None:
            self.root.after_cancel(self._hl_job)
        self._hl_job = self.root.after(150, self._highlight)

    def _highlight(self, widget=None):
        self._hl_job = None
        widget = widget or self.sql_text
        text = widget.get("1.0", "end-1c")
        for tag in ("kw", "num", "str", "com"):
            widget.tag_remove(tag, "1.0", tk.END)

        def apply(tag, regex):
            for m in regex.finditer(text):
                widget.tag_add(tag, f"1.0+{m.start()}c", f"1.0+{m.end()}c")

        apply("kw", KEYWORD_RE)
        apply("num", NUMBER_RE)
        apply("str", STRING_RE)
        apply("com", COMMENT_RE)
        # string ve yorum, anahtar kelime renginin üstünde kalsın
        widget.tag_raise("str")
        widget.tag_raise("com")

    # ----------------------------------------------------- Otomatik tamamlama

    def _current_word(self):
        line = self.sql_text.get("insert linestart", "insert")
        m = WORD_RE.search(line)
        return m.group(0) if m else ""

    def _show_autocomplete(self, items):
        if not items:
            self._hide_autocomplete()
            return
        self.ac_list.delete(0, tk.END)
        for it in items[:100]:
            self.ac_list.insert(tk.END, it)
        self.ac_list.selection_set(0)
        self.ac_list.activate(0)
        bbox = self.sql_text.bbox("insert")
        if not bbox:
            self._hide_autocomplete()
            return
        x = self.sql_text.winfo_rootx() + bbox[0]
        y = self.sql_text.winfo_rooty() + bbox[1] + bbox[3] + 2
        height = min(8, len(items))
        self.ac_list.configure(height=height)
        self.ac_win.geometry(f"+{x}+{y}")
        self.ac_win.deiconify()
        self.ac_win.lift()

    def _hide_autocomplete(self):
        self.ac_win.withdraw()

    def _ac_visible(self):
        return self.ac_win.state() == "normal"

    def _accept_autocomplete(self):
        sel = self.ac_list.curselection()
        if not sel:
            self._hide_autocomplete()
            return
        completion = self.ac_list.get(sel[0])
        word = self._current_word()
        if word:
            self.sql_text.delete(f"insert-{len(word)}c", "insert")
        self.sql_text.insert("insert", completion)
        self._hide_autocomplete()
        self._schedule_highlight()

    def _update_autocomplete(self):
        word = self._current_word()
        if len(word) < 2:
            self._hide_autocomplete()
            return
        wl = word.lower()
        matches = sorted(
            {c for c in self.completions if c.lower().startswith(wl)},
            key=lambda s: (len(s), s))
        matches = [m for m in matches if m.lower() != wl]
        self._show_autocomplete(matches)

    def _force_autocomplete(self, _event):
        word = self._current_word()
        wl = word.lower()
        matches = sorted(
            {c for c in self.completions if c.lower().startswith(wl)},
            key=lambda s: (len(s), s)) if word else sorted(self.completions)
        self._show_autocomplete(matches)
        return "break"

    def _on_editor_keypress(self, event):
        if not self._ac_visible():
            return None
        if event.keysym in ("Down", "Up"):
            size = self.ac_list.size()
            cur = self.ac_list.curselection()
            idx = cur[0] if cur else 0
            idx = min(size - 1, idx + 1) if event.keysym == "Down" else max(0, idx - 1)
            self.ac_list.selection_clear(0, tk.END)
            self.ac_list.selection_set(idx)
            self.ac_list.activate(idx)
            self.ac_list.see(idx)
            return "break"
        if event.keysym in ("Return", "Tab"):
            self._accept_autocomplete()
            return "break"
        if event.keysym == "Escape":
            self._hide_autocomplete()
            return "break"
        return None

    def _on_editor_keyrelease(self, event):
        if event.keysym in ("Down", "Up", "Return", "Tab", "Escape",
                            "Left", "Right", "Meta_L", "Meta_R",
                            "Control_L", "Control_R", "Shift_L", "Shift_R"):
            return
        self._schedule_highlight()
        if event.keysym == "period":
            self._on_dot_trigger()
        elif event.keysym == "BackSpace" or (event.char and
                                             re.match(r"[\w$#]", event.char)):
            self._update_autocomplete()
        else:
            self._hide_autocomplete()

    def _on_dot_trigger(self):
        """table. yazılınca kolon önerisi göster."""
        line = self.sql_text.get("insert linestart", "insert")
        m = re.search(r"([A-Za-z0-9_$#]+)\.$", line)
        if not m:
            self._hide_autocomplete()
            return
        name = m.group(1).upper()
        if name in self.columns_cache:
            self._show_autocomplete(self.columns_cache[name])
        elif name in self.known_objects:
            self._fetch_columns(self.known_objects[name], name, show_popup=True)
        elif name in self.schema_objects:
            # şema adı yazıldı: o şemanın nesnelerini öner
            objs = self.schema_objects.get(name)
            if objs:
                self._show_autocomplete([n for _t, n in objs])
        else:
            self._hide_autocomplete()

    def _fetch_columns(self, schema, name, show_popup=False):
        if self.conn is None or self.busy or name in self.columns_cache:
            if show_popup and name in self.columns_cache:
                self._show_autocomplete(self.columns_cache[name])
            return

        def work(conn):
            cur = conn.cursor()
            cur.execute(
                "SELECT column_name FROM all_tab_columns "
                "WHERE owner = :o AND table_name = :t ORDER BY column_id",
                o=schema, t=name)
            cols = [r[0] for r in cur.fetchall()]
            cur.close()
            return ("columns", name, cols, show_popup)
        self._run_in_thread(work)

    # ------------------------------------------------------------ Tarihçe

    def _save_history(self):
        self._save_json(HISTORY_FILE, self.history[:HISTORY_LIMIT])

    def _add_history(self, sql):
        # Aynı sorgu üst üste tekrarlanırsa tek kayıt tut
        if self.history and self.history[0]["sql"] == sql:
            self.history[0]["time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        else:
            self.history.insert(0, {
                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "sql": sql,
            })
            del self.history[HISTORY_LIMIT:]
        self._save_history()
        self._render_history()

    def _render_history(self):
        self.hist_tree.delete(*self.hist_tree.get_children())
        for i, item in enumerate(self.history):
            snippet = " ".join(item["sql"].split())
            if len(snippet) > 120:
                snippet = snippet[:120] + "…"
            self.hist_tree.insert("", tk.END, iid=str(i),
                                  values=(item["time"], snippet))

    def _on_history_open(self, _event):
        item = self.hist_tree.focus()
        if not item:
            return
        sql = self.history[int(item)]["sql"]
        self.new_tab(sql=sql)

    def _clear_history(self):
        if not self.history:
            return
        if messagebox.askyesno("Tarihçeyi temizle",
                               "Tüm sorgu tarihçesi silinsin mi?"):
            self.history = []
            self._save_history()
            self._render_history()

    # ------------------------------------------------- Thread altyapısı

    def _run_in_thread(self, func):
        self.busy = True

        def work():
            try:
                self.result_queue.put(func(self.conn))
            except Exception as exc:
                self.result_queue.put(("error", str(exc)))

        threading.Thread(target=work, daemon=True).start()

    def _poll_queue(self):
        try:
            while True:
                msg = self.result_queue.get_nowait()
                self._handle_message(msg)
        except queue.Empty:
            pass
        self.root.after(100, self._poll_queue)

    def _next_pending_load(self):
        """Yenileme sonrası açık şemaları sırayla yeniden yükle."""
        while self.pending_loads:
            schema = self.pending_loads.pop(0)
            if schema in self.schemas and self.schema_objects.get(schema) is None:
                self._load_schema_objects(schema)
                return True
        return False

    def _handle_message(self, msg):
        kind = msg[0]
        if kind == "connected":
            self.conn = msg[1]
            self.disconnect_btn.configure(state=tk.NORMAL)
            self.run_btn.configure(state=tk.NORMAL)
            self.refresh_btn.configure(state=tk.NORMAL)
            self.schema_btn.configure(state=tk.NORMAL)
            self.ddl_btn.configure(state=tk.NORMAL)
            self._set_status(f"Bağlandı: {self.user_var.get()}@{self.host_var.get()}"
                             f":{self.port_var.get()}/{self.service_var.get()}")
            self.load_schemas()
        elif kind == "connect_error":
            self.connect_btn.configure(state=tk.NORMAL)
            self._set_status("Bağlantı hatası")
            messagebox.showerror("Bağlantı hatası", msg[1])
        elif kind == "schemas":
            self.busy = False
            self.schemas = msg[1]
            self.completions.update(self.schemas)
            self._render_tree()
            if not self._next_pending_load():
                # ilk bağlantı: kullanıcının kendi şemasını otomatik yükle
                current_user = self.user_var.get().strip().upper()
                if (current_user in self.schemas
                        and self.schema_objects.get(current_user) is None):
                    self.open_schemas.add(current_user)
                    self._load_schema_objects(current_user)
                else:
                    self._set_status(f"{len(self.schemas)} şema listelendi")
        elif kind == "objects":
            self.busy = False
            _, schema, objs = msg
            self.schema_objects[schema] = objs
            for _otype, name in objs:
                self.known_objects[name] = schema
            self.completions.update(n for _t, n in objs)
            self._render_tree()
            if not self._next_pending_load():
                self._set_status(f"{schema}: {len(objs)} nesne listelendi")
        elif kind == "rows":
            self.busy = False
            _, tab, columns, rows, truncated, elapsed = msg
            if tab in self.tabs:
                tab.show_rows(columns, rows)
                tab.last_truncated = truncated
                tab.last_elapsed = elapsed
                self.completions.update(columns)
            note = f" (ilk {MAX_QUERY_ROWS} satır gösteriliyor)" if truncated else ""
            self._set_status(f"[{tab.title}] {len(rows)} satır — {elapsed:.2f} sn{note}")
            self.run_btn.configure(state=tk.NORMAL)
            self._update_export_btn()
        elif kind == "dml_done":
            self.busy = False
            _, tab, affected, elapsed = msg
            if tab in self.tabs:
                tab.clear_grid()
            self._set_status(f"[{tab.title}] İfade çalıştı: {affected} satır etkilendi"
                             f" — {elapsed:.2f} sn (commit edildi)")
            self.run_btn.configure(state=tk.NORMAL)
            self._update_export_btn()
        elif kind == "columns":
            self.busy = False
            _, name, cols, show_popup = msg
            self.columns_cache[name] = cols
            self.completions.update(cols)
            if show_popup and cols:
                self._show_autocomplete(cols)
        elif kind == "ddl_results":
            self.busy = False
            _, term, results = msg
            if self.conn:
                self.ddl_btn.configure(state=tk.NORMAL)
            self._show_ddl_results(term, results)
            self._set_status(f"DDL araması '{term}': {len(results)} eşleşme")
            self._update_export_btn()
        elif kind == "object_ddl":
            self.busy = False
            _, owner, name, ddl = msg
            if ddl.strip():
                self.new_tab(sql=ddl, title=name)
                self._set_status(f"{owner}.{name} kaynağı açıldı")
            else:
                self._set_status(f"{owner}.{name} için kaynak bulunamadı")
        elif kind == "error":
            self.busy = False
            self.pending_loads = []
            self.run_btn.configure(state=tk.NORMAL if self.conn else tk.DISABLED)
            self.ddl_btn.configure(state=tk.NORMAL if self.conn else tk.DISABLED)
            self._set_status("Hata")
            messagebox.showerror("Hata", msg[1])

    def _set_status(self, text):
        self.status_var.set(text)

    def _on_close(self):
        if self.conn is not None:
            try:
                self.conn.close()
            except Exception:
                pass
        self.root.destroy()


def main():
    root = tk.Tk()
    OracleClientApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
