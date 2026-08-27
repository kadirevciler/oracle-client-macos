#!/usr/bin/env python3
"""Basit Oracle SQL istemcisi — macOS masaüstü uygulaması.

Gereksinim: pip3 install oracledb  (thin mode, Oracle Instant Client gerekmez)
Çalıştırma: python3 oracle_client.py
"""

import base64
import csv
import json
import os
import queue
import re
import threading
import time
from datetime import datetime
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
        self.known_objects = {}         # NESNE_ADI -> şema (intellisense için)
        self.columns_cache = {}         # NESNE_ADI -> [kolonlar]
        self.completions = set(SQL_KEYWORDS)

        self.last_columns = None        # CSV export için son sonuç
        self.last_rows = None
        self.script_path = None         # açık betik dosyası
        self._hl_job = None             # renklendirme debounce
        self.profiles = self._load_json(PROFILES_FILE, {})

        self._build_connection_bar()
        self._build_main_area()
        self._build_status_bar()
        self._build_autocomplete()

        self.root.after(100, self._poll_queue)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        # macOS: pencereyi öne getir ve odakla (arka planda odaksız açılmasın)
        self.root.lift()
        self.root.attributes("-topmost", True)
        self.root.after(300, lambda: self.root.attributes("-topmost", False))
        self.root.focus_force()

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

        self.filter_var = tk.StringVar()
        self.filter_var.trace_add("write", lambda *a: self._apply_filter())
        filter_entry = ttk.Entry(left, textvariable=self.filter_var)
        filter_entry.pack(fill=tk.X, pady=(0, 4))
        self._add_placeholder(filter_entry, "Filtrele...")

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

        # ---- Sağ panel: SQL editörü + sonuç grid'i
        right = ttk.PanedWindow(outer, orient=tk.VERTICAL)
        outer.add(right, weight=4)

        editor_frame = ttk.Frame(right)
        right.add(editor_frame, weight=1)

        editor_top = ttk.Frame(editor_frame)
        editor_top.pack(fill=tk.X, pady=(0, 2))
        self.run_btn = ttk.Button(editor_top, text="▶ Çalıştır (⌘↩)",
                                  command=self.run_query, state=tk.DISABLED)
        self.run_btn.pack(side=tk.LEFT)
        ttk.Separator(editor_top, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=6)
        ttk.Button(editor_top, text="📂 Aç",
                   command=self.open_script).pack(side=tk.LEFT, padx=2)
        ttk.Button(editor_top, text="💾 Kaydet (⌘S)",
                   command=self.save_script).pack(side=tk.LEFT, padx=2)
        ttk.Button(editor_top, text="Farklı Kaydet",
                   command=lambda: self.save_script(save_as=True)).pack(side=tk.LEFT, padx=2)
        ttk.Separator(editor_top, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=6)
        self.export_btn = ttk.Button(editor_top, text="⬇ CSV'ye Aktar",
                                     command=self.export_csv, state=tk.DISABLED)
        self.export_btn.pack(side=tk.LEFT, padx=2)
        ttk.Label(editor_top, text=f"  ^Space: tamamlama — ilk {MAX_QUERY_ROWS} satır"
                  ).pack(side=tk.LEFT)

        editor_body = ttk.Frame(editor_frame)
        editor_body.pack(fill=tk.BOTH, expand=True)
        self.sql_text = tk.Text(editor_body, height=8, wrap=tk.NONE,
                                font=("Menlo", 13), undo=True,
                                background="#ffffff", foreground="#1f1f1f",
                                insertbackground="#1f1f1f")
        sql_scroll = ttk.Scrollbar(editor_body, orient=tk.VERTICAL,
                                   command=self.sql_text.yview)
        self.sql_text.configure(yscrollcommand=sql_scroll.set)
        sql_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.sql_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        # renklendirme etiketleri
        self.sql_text.tag_configure("kw", foreground="#0033b3", font=("Menlo", 13, "bold"))
        self.sql_text.tag_configure("num", foreground="#a34a00")
        self.sql_text.tag_configure("str", foreground="#067d17")
        self.sql_text.tag_configure("com", foreground="#8c8c8c",
                                    font=("Menlo", 13, "italic"))

        self.sql_text.bind("<Command-Return>", lambda e: (self.run_query(), "break")[1])
        self.sql_text.bind("<F5>", lambda e: (self.run_query(), "break")[1])
        self.sql_text.bind("<Command-s>", lambda e: (self.save_script(), "break")[1])
        self.sql_text.bind("<Command-o>", lambda e: (self.open_script(), "break")[1])
        self.sql_text.bind("<Control-space>", self._force_autocomplete)
        self.sql_text.bind("<KeyPress>", self._on_editor_keypress)
        self.sql_text.bind("<KeyRelease>", self._on_editor_keyrelease)
        self.sql_text.bind("<Button-1>", lambda e: self._hide_autocomplete())

        grid_frame = ttk.Frame(right)
        right.add(grid_frame, weight=3)

        self.grid = ttk.Treeview(grid_frame, show="headings", selectmode="extended")
        grid_y = ttk.Scrollbar(grid_frame, orient=tk.VERTICAL, command=self.grid.yview)
        grid_x = ttk.Scrollbar(grid_frame, orient=tk.HORIZONTAL, command=self.grid.xview)
        self.grid.configure(yscrollcommand=grid_y.set, xscrollcommand=grid_x.set)
        grid_y.pack(side=tk.RIGHT, fill=tk.Y)
        grid_x.pack(side=tk.BOTTOM, fill=tk.X)
        self.grid.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

    def _build_history_tab(self, parent):
        top = ttk.Frame(parent)
        top.pack(fill=tk.X, pady=(0, 4))
        ttk.Button(top, text="Temizle", command=self._clear_history).pack(side=tk.RIGHT)
        ttk.Label(top, text="Çift tık: editöre yükle").pack(side=tk.LEFT)

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
        self.schemas = []
        self.schema_objects = {}
        self.open_schemas = set()
        self.known_objects = {}
        self.columns_cache = {}
        self.completions = set(SQL_KEYWORDS)
        self.obj_tree.delete(*self.obj_tree.get_children())
        self._clear_grid()
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
        if filt == "Filtrele...":
            filt = ""
        filt = filt.upper()

        for schema in self.schemas:
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
        self.sql_text.delete("1.0", tk.END)
        self.sql_text.insert("1.0", sql)
        self._highlight()
        self.run_query()
        self._fetch_columns(schema, name)

    # ------------------------------------------------------------- Sorgu

    def run_query(self):
        if self.conn is None or self.busy:
            return
        self._hide_autocomplete()
        sql = self.sql_text.get("1.0", tk.END).strip().rstrip(";")
        if not sql:
            return
        self._add_history(sql)
        self._set_status("Sorgu çalışıyor...")
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
                    return ("dml_done", affected, elapsed)
                columns = [d[0] for d in cur.description]
                rows = cur.fetchmany(MAX_QUERY_ROWS)
                truncated = cur.fetchone() is not None
                elapsed = time.perf_counter() - start
                return ("rows", columns, rows, truncated, elapsed)
            finally:
                cur.close()
        self._run_in_thread(work)

    # ------------------------------------------------------------- Grid

    def _clear_grid(self):
        self.grid.delete(*self.grid.get_children())
        self.grid.configure(columns=())
        self.last_columns = None
        self.last_rows = None
        self.export_btn.configure(state=tk.DISABLED)

    def _show_rows(self, columns, rows):
        self._clear_grid()
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
        if rows or columns:
            self.export_btn.configure(state=tk.NORMAL)
        # kolon adlarını tamamlama önerilerine ekle
        self.completions.update(columns)

    def export_csv(self):
        if not self.last_columns:
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
                writer.writerow(self.last_columns)
                for row in self.last_rows:
                    writer.writerow(["" if v is None else v for v in row])
            self._set_status(f"{len(self.last_rows)} satır CSV'ye aktarıldı: {path}")
        except OSError as exc:
            messagebox.showerror("CSV hatası", str(exc))

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
        self.sql_text.delete("1.0", tk.END)
        self.sql_text.insert("1.0", content)
        self._highlight()
        self.script_path = path
        self.root.title(f"Oracle Client — {os.path.basename(path)}")
        self._set_status(f"Betik açıldı: {path}")

    def save_script(self, save_as=False):
        if save_as or not self.script_path:
            path = filedialog.asksaveasfilename(
                title="SQL betiğini kaydet", defaultextension=".sql",
                filetypes=[("SQL dosyası", "*.sql"), ("Tüm dosyalar", "*.*")],
                initialfile=os.path.basename(self.script_path)
                if self.script_path else "betik.sql",
                parent=self.root)
            if not path:
                return
            self.script_path = path
        try:
            with open(self.script_path, "w", encoding="utf-8") as f:
                f.write(self.sql_text.get("1.0", "end-1c"))
        except OSError as exc:
            messagebox.showerror("Dosya hatası", str(exc))
            return
        self.root.title(f"Oracle Client — {os.path.basename(self.script_path)}")
        self._set_status(f"Betik kaydedildi: {self.script_path}")

    # ----------------------------------------------- Sözdizimi renklendirme

    def _schedule_highlight(self):
        if self._hl_job is not None:
            self.root.after_cancel(self._hl_job)
        self._hl_job = self.root.after(150, self._highlight)

    def _highlight(self):
        self._hl_job = None
        text = self.sql_text.get("1.0", "end-1c")
        for tag in ("kw", "num", "str", "com"):
            self.sql_text.tag_remove(tag, "1.0", tk.END)

        def apply(tag, regex):
            for m in regex.finditer(text):
                self.sql_text.tag_add(tag, f"1.0+{m.start()}c", f"1.0+{m.end()}c")

        apply("kw", KEYWORD_RE)
        apply("num", NUMBER_RE)
        apply("str", STRING_RE)
        apply("com", COMMENT_RE)
        # string ve yorum, anahtar kelime renginin üstünde kalsın
        self.sql_text.tag_raise("str")
        self.sql_text.tag_raise("com")

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
        self.sql_text.delete("1.0", tk.END)
        self.sql_text.insert("1.0", sql)
        self._highlight()

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

    def _handle_message(self, msg):
        kind = msg[0]
        if kind == "connected":
            self.conn = msg[1]
            self.disconnect_btn.configure(state=tk.NORMAL)
            self.run_btn.configure(state=tk.NORMAL)
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
            # bağlanılan kullanıcının şemasını otomatik yükle
            current_user = self.user_var.get().strip().upper()
            if current_user in self.schemas:
                self.open_schemas.add(current_user)
                self._load_schema_objects(current_user)
            self._set_status(f"{len(self.schemas)} şema listelendi")
        elif kind == "objects":
            self.busy = False
            _, schema, objs = msg
            self.schema_objects[schema] = objs
            for _otype, name in objs:
                self.known_objects[name] = schema
            self.completions.update(n for _t, n in objs)
            self._render_tree()
            self._set_status(f"{schema}: {len(objs)} nesne listelendi")
        elif kind == "rows":
            self.busy = False
            _, columns, rows, truncated, elapsed = msg
            self._show_rows(columns, rows)
            note = f" (ilk {MAX_QUERY_ROWS} satır gösteriliyor)" if truncated else ""
            self._set_status(f"{len(rows)} satır — {elapsed:.2f} sn{note}")
            self.run_btn.configure(state=tk.NORMAL)
        elif kind == "dml_done":
            self.busy = False
            _, affected, elapsed = msg
            self._clear_grid()
            self._set_status(f"İfade çalıştı: {affected} satır etkilendi — "
                             f"{elapsed:.2f} sn (commit edildi)")
            self.run_btn.configure(state=tk.NORMAL)
        elif kind == "columns":
            self.busy = False
            _, name, cols, show_popup = msg
            self.columns_cache[name] = cols
            self.completions.update(cols)
            if show_popup and cols:
                self._show_autocomplete(cols)
        elif kind == "error":
            self.busy = False
            self.run_btn.configure(state=tk.NORMAL if self.conn else tk.DISABLED)
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
