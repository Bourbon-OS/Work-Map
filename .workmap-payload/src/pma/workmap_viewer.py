"""Local desktop view and permitted edits, using one committed snapshot.

Local window preferences remember source locations, never Map contents or
authority. No server, network client, cached snapshot or second Store.
"""
from __future__ import annotations

import argparse

import os
import json
import tempfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import ttk, filedialog
from pma.workmap_viewer_ui import (SectionPane, FoldSection, actor_label, actor_code, pin_badges, actor_assignments, people_groups, PAPER, INK, MUTED, DARK, ACCENT)

from pma.workmap_desktop_extension import desktop_extension, extension_available
from pma.workmap_store_types import StoreError
from pma.workmap_viewer_refresh import (file_marks, read_marker, map_inputs, thread_inputs,
                                       pane_state, restore_folds)
from pma.workmap_actor_names import local_actor_names, local_task_index_path
MAP_ADD_OPERATION = "add_work_in_map"


@dataclass
class OpenMap:
    path: Path
    evidence_root: Path
    project_id: str | None = None
    state_trial: bool | None = False
    map_id: str | None = None
    require_current: bool = False
    state_only: bool = False


def viewer_session_path() -> Path:
    return Path(os.environ.get('LOCALAPPDATA', Path.home() / '.local' / 'share')) / 'Ashiba' / 'WorkMap' / 'viewer-session.json'


def read_session(path: Path):
    """Locations and identity bindings only; do not read any source here."""
    value = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(value, dict) or value.get('version') != 1 or not isinstance(value.get('maps'), list):
        raise ValueError('保存したMap一覧の形式を確認できません。')
    sources = []
    for item in value['maps']:
        if not isinstance(item, dict) or set(item) != {'path', 'evidence_root', 'project_id'}:
            raise ValueError('保存したMapの接続情報を確認できません。')
        if any(not isinstance(item[k], str) or not item[k].strip() for k in item):
            raise ValueError('保存したMapの接続情報が空です。')
        paths = [Path(item[k]) for k in ('path', 'evidence_root')]
        if any(not p.is_absolute() or str(p).startswith(('\\\\', '//')) for p in paths):
            raise ValueError('保存したMapの場所はPC内の絶対パスに限ります。')
        if any(s.path == paths[0] for s in sources):
            raise ValueError('保存したMap一覧に重複があります。')
        sources.append(OpenMap(*paths, item['project_id']))
    selected = value.get('selected')
    if selected is not None and (not isinstance(selected, str) or not any(str(s.path) == selected for s in sources)):
        raise ValueError('最後に選んだMapを確認できません。')
    return sources, selected


def _check_source_paths(source):
    for path in (source.path, source.evidence_root):
        if str(path).startswith(('\\\\', '//')):
            raise ValueError('この版は同じPC内の保存先が対象です。ネットワーク共有は開きません。')
        if os.name == 'nt':
            import ctypes
            if ctypes.windll.kernel32.GetDriveTypeW(str(path.anchor)) == 4:
                raise ValueError('ネットワークドライブは対象外です。PC内の正本を選んでください。')


def state_source(source: OpenMap) -> bool:
    if source.state_only or not extension_available():
        return True
    if source.state_trial is not None:
        return source.state_trial
    from pma.workmap_normal_entry import is_state_store
    return is_state_store(source.path)


def state_source_read(source: OpenMap):
    from pma.workmap_state_read import read_trial_state
    read = read_trial_state(source.path, project_id=source.project_id, map_id=source.map_id)
    if source.require_current and read.snapshot['metadata']['store_role'] != 'canonical_current':
        raise ValueError('現在の正本として宣言されているDBではありません。')
    return read


def read_map_title(source: OpenMap) -> str:
    """Read only a menu label, not a verified snapshot or any decision data."""
    _check_source_paths(source)
    if state_source(source):
        snapshot = state_source_read(source).snapshot
        children = {r['child'] for r in snapshot['map']['relations'] if r['kind'] == 'parent'}
        names = [snapshot['works'][k]['name'] for k in snapshot['map']['members'] if k not in children]
        return ' / '.join(names) or snapshot['project_id']
    return desktop_extension().read_title(source)

def _read_titles(sources):
    result = []
    for source in sources:
        try:
            result.append((source.path, source.project_id, read_map_title(source)))
        except (OSError, ValueError, StoreError):
            result.append((source.path, source.project_id, None))
    return result


def load_map(source: OpenMap) -> dict:
    """Reject absent/corrupt/replaced/noncurrent data; never initialize a DB."""
    _check_source_paths(source)
    file_before = file_marks((source.path,))
    if state_source(source):
        committed = state_source_read(source)
        return {'snapshot': committed.snapshot,
                'thread': {'status': 'confirmed', 'thread_entries': committed.snapshot['thread_entries']},
                'selection_paths': (), 'refresh_marker': (committed.token, (), file_before),
                'read_at': datetime.now().astimezone().isoformat(timespec='seconds')}
    return desktop_extension().load_map(source, file_before)


LABELS = {'summary': '概要', 'refs': '参照', 'source_refs': '根拠', 'why': '理由',
          'consequence': '分かったこと・結果', 'revisit': '再検討条件',
          'outcome': '終了種別', 'identity': '終了時のWork', 'intent': '目的',
          'origin': '由来', 'name': '名前', 'display_id': '住所', 'work_key': '識別子',
          'actor': '主体', 'actor_id': '主体', 'grantee': '許可対象の主体',
          'operations': '許可する操作', 'constraints': '許可の範囲・制約',
          'effect': '許可判断', 'targets': '対象', 'text': '内容', 'basis': '根拠の区分',
          'id': '記録ID', 'grantor_ref': '許可元', 'return_work': '戻るWork',
          'remaining': '残ること', 'first_step': '再開時の一手',
          'unsaved': '未保存・未確認', 'reusable_refs': '再利用できる根拠',
          'recorded_at': '記録日時', 'occurred_at': '出来事の日時',
          'successor': '後継Work', 'residuals': '引き継いだ／残した範囲'}


def current_record_text(records, snapshot, collection, actor_names=None, *, store_path=None):
    """Put the authored rule/permission first; keep all raw fields foldable."""
    blocks = []
    for record in records:
        targets = ' / '.join(snapshot['display_ids'].get(k, k) for k in record['targets'])
        if collection == 'action_rules':
            lines = [record['statement'], '対象Work：' + targets]
            if record.get('applies_to'):
                lines.append('適用する場面：' + describe(record['applies_to']))
            if record.get('clears_when'):
                lines.append('外せる条件：' + record['clears_when'])
        else:
            operations = {'review': 'レビュー', 'implement_isolated_core': '隔離した実装',
                          'run_isolated_tests': '隔離試験', 'append_thread': 'Threadへの記録',
                          'append_scoped_thread': '指定範囲のThreadへの記録',
                          'update_pin': 'Pinの更新', 'remove_pin': 'Pinの解除',
                          'remove_permission': '許可の終了', 'close': 'Workの終了',
                          'add_work': 'Workの追加', 'add_work_in_map': 'Map全体のWork追加', 'reparent': '親Workの変更'}
            lines = [{'allow': '許可', 'deny': '禁止', 'unknown': '未確認'}.get(record['effect'], '未確認'),
                     '対象Work：' + targets,
                     '対象の担当：' + actor_label(record['grantee'], actor_names),
                     '操作：' + describe([operations.get(op, op) for op in record['operations']])]
            if MAP_ADD_OPERATION in record.get('operations', []):
                lines[1] = '対象範囲：この正本Mapの現在・今後の全Workを親にした新規登録'
                lines.append('範囲の基点：' + targets + '（普通の親Workの許可継承ではありません）')
                if snapshot['format_version'] == 'workmap-state-1':
                    lines[1] = '対象範囲：Map全体への追加許可の記録（この画面は閲覧専用）'
                elif record not in desktop_extension().map_add_permissions(snapshot, record['targets'][0], store_path=store_path):
                    lines.insert(0, '保存先・project・Map構造が一致しないため、このMap全体の許可は適用しません。')
            if record.get('constraints'):
                lines.append('範囲・制約：' + describe(record['constraints']))
        blocks.append('\n\n'.join(lines))
    return '\n\n────────\n\n'.join(blocks) or '記録なし'


def describe(value, indent=0):
    """Display text as text, including unknown and references. No link execution."""
    if value is None:
        return '未確認'
    if isinstance(value, str):
        if value.startswith('{'):
            try:
                return describe(json.loads(value), indent)
            except ValueError:
                pass
        return value
    if isinstance(value, list):
        return '\n'.join('・' + describe(x, indent + 1) for x in value) or '記録なし'
    if isinstance(value, dict):
        return '\n'.join('  ' * indent + LABELS.get(k, k) + '：' + describe(v, indent + 1)
                         for k, v in value.items())
    return str(value)


def detail_context(snapshot, key):
    if snapshot['format_version'] != 'workmap-state-1':
        return desktop_extension().inspect_work(snapshot, key)
    from pma.workmap_state_contract import chain, collection_status
    path = chain(snapshot, key)
    return {
        'parent_chain': [dict(snapshot['works'][k], display_id=snapshot['display_ids'][k]) for k in path],
        'pin_status': collection_status(snapshot, 'pins', key),
        'permission_status': collection_status(snapshot, 'permissions', key),
        'action_rule_status': collection_status(snapshot, 'action_rules', key),
        'children': [dict(snapshot['works'][r['child']], display_id=snapshot['display_ids'][r['child']])
                     for r in snapshot['map']['relations'] if r['kind'] == 'parent' and r['parent'] == key],
        'dependencies': [r for r in snapshot['map']['relations'] if r['kind'] == 'dependency'
                         and key in (r['consumer'], r['provider'])],
    }


def detail_results(snapshot, key):
    if snapshot['format_version'] == 'workmap-state-1':
        return [e for e in snapshot['evidence'] if key in e['works']], []
    return desktop_extension().detail_results(snapshot, key)


def work_details(data: dict, key: str) -> str:
    s = data['snapshot']; context = detail_context(s, key); work = s['works'][key]
    chain = context['parent_chain']
    title = lambda k: s['display_ids'].get(k, k) + ' ' + s['works'].get(k, {}).get('name', '')
    blocks = [title(key), '目的\n' + describe(work['intent']),
              '由来\n' + describe(work['origin']),
              '親への道筋\n' + ' → '.join(p['display_id'] + ' ' + p['name'] for p in chain)]
    children = [r['child'] for r in s['map']['relations'] if r['kind'] == 'parent' and r['parent'] == key]
    blocks.append('子Work\n' + ('\n'.join(title(k) for k in children) or 'なし'))
    dependencies = [r for r in s['map']['relations'] if r['kind'] == 'dependency' and key in (r['consumer'], r['provider'])]
    blocks.append('依存関係\n' + ('\n'.join(title(r['consumer']) + ' → 必要とする → ' + title(r['provider']) + '\n' + describe(r) for r in dependencies) or '記録なし'))
    for collection, status_name, heading in [('pins', 'pin_status', '現在の関与（Pin）'),
                                             ('permissions', 'permission_status', '許可（Permission）'),
                                             ('action_rules', 'action_rule_status', '行動ルール（Action Rule）')]:
        known = context[status_name] == 'confirmed'
        records = [r for r in s['map'][collection] if (r['work_id'] == key if collection == 'pins' else key in r['targets'])]
        status = '確認済み' if known else '集合の完全性は未確認。以下は正本に記録された分のみ。'
        blocks.append(heading + '\n' + status + '\n' + describe(records))
    ancestors = {p['work_key'] for p in chain[:-1]}
    inherited = [r for name in ('permissions', 'action_rules') for r in s['map'][name] if ancestors.intersection(r['targets'])]
    blocks.append('親側の許可・制約の記録（適用範囲の確認が必要）\n' + describe(inherited))
    results, hints = detail_results(s, key)
    blocks.append('成果・根拠の記録（最新＝現在状態ではありません）\n' + describe(results))
    if hints:
        blocks.append('担当の過去の残し書き（回復補助・現在への適用は要確認）\n' + describe(hints))
    blocks.append('識別子\n' + key + '\n取得した版\n' + s['revision'])
    return '\n\n'.join(blocks)


def thread_work_labels(data: dict) -> dict:
    """Use current identity or the selected close history; never infer a Work."""
    s = data['snapshot']
    labels = {k: {'label': s['display_ids'][k], 'name': s['works'][k]['name']}
              for k in s['map']['members']}
    for entry in data['thread']['thread_entries']:
        for k in entry['works']:
            if k in labels or s['retirements'].get(k) not in (entry['id'], *entry.get('related_entries', [])):
                continue
            identity = entry.get('identity', {})
            if identity and identity.get('work_key') != k:
                continue
            address = entry.get('display_id')
            if s['format_version'] == 'workmap-state-1' and s['retirements'].get(k) == entry['id']:
                address = s['display_ids'][k]
            if address and len(entry['works']) == 1:
                labels[k] = {'label': address + '（終了済み）',
                             'name': identity.get('name', '')}
    for k in s['retirements']:
        labels.setdefault(k, {'label': s['display_ids'].get(k, k) + '（終了済み）', 'name': ''})
    return labels


def thread_entries(data: dict, key=None, query='') -> list:
    """Search only the normal meaning projection, not technical/pending rows."""
    labels = thread_work_labels(data); query = query.strip().casefold()
    entries = [e for e in data['thread']['thread_entries'] if key is None or key in e['works']]
    if not query:
        return entries
    return [e for e in entries if query in (' '.join(
        labels.get(k, {'label': k, 'name': ''})['label'] + ' ' + labels.get(k, {}).get('name', '')
        for k in e['works']) + '\n' + describe(e)).casefold()]


def thread_text(data: dict, key: str | None = None) -> str:
    selection = data['thread']; s = data['snapshot']
    state = selection['status']
    banner = {'confirmed': '意味Thread：確認済み', 'needs_review': '意味Thread：選別待ちの記録あり',
              'unknown': '意味Thread：未確認。必要な根拠を読めていません。'}[state]
    if state == 'unknown':
        return banner + '\n\n' + selection.get('reason', '') + '\n\n「理由の根拠フォルダ」で、この正本に対応する資料のルートを選んで再読してください。'
    blocks = [banner + ('\nプロジェクト全体の選別待ち：' + str(selection['unreviewed_count']) if state == 'needs_review' else '')]
    labels = thread_work_labels(data)
    for entry in thread_entries(data, key):
        names = ' / '.join(labels.get(k, {'label': k})['label'] for k in entry['works'])
        block = [names + '  ' + entry['summary'], entry['why']]
        for field in ('identity', 'outcome', 'successor', 'residuals', 'consequence', 'revisit', 'source_refs'):
            if field in entry:
                block.append(LABELS.get(field, field) + '\n' + describe(entry[field]))
        # Recover chronology only on request through the existing raw IDs. Do
        # not mistake projection order or recording time for event time.
        raw = {e['id']: e for e in s['thread_entries']}
        related = entry.get('related_entries', [])
        references = [entry['id'], *related] if s['format_version'] == 'workmap-state-1' else related
        provenance = [raw[k] for k in dict.fromkeys(references) if k in raw]
        block.append('関連する記録のID\n' + describe(related))
        block.append('参照する記録（ID：出来事／記録日時）\n' + '\n'.join(
            e['id'] + '：' + describe(e.get('occurred_at')) + ' ／ ' + describe(e.get('recorded_at'))
            + '\n記録した担当：' + describe(e.get('actor')) for e in provenance))
        blocks.append('\n\n'.join(block))
    return '\n\n' + ('\n\n' + '─' * 32 + '\n\n').join(blocks) + ('\n\nこの範囲の意味Threadはありません。' if len(blocks) == 1 else '')


class WorkMapViewer:
    def __init__(self, root: tk.Tk, current_actor: str | None = None, actor_names: dict | None = None,
                 operator_id: str | None = None, session_path: Path | None = None,
                 background_reads: bool = False, task_index_path: Path | None = None,
                 state_trial: bool = False, trial_write: bool = False, shared_formats: bool = False, state_only: bool = False):
        self.root = root; self.sources: list[OpenMap] = []; self.current = -1
        self.actor_names = dict(actor_names or {})
        self.task_index_path = task_index_path; self.resolved_actor_names = {}
        self.operator_id = operator_id
        self.actor_codes = {}
        self.data = None; self.focus_key = None; self.request_number = 0
        self.all_thread_dirty = True
        self.project_names = {}; self.open_nodes = {}; self.last_detail = None
        self.state_trial = state_trial
        self.shared_formats = shared_formats
        self.state_only = state_only or not extension_available()
        self.trial_write = state_trial and trial_write
        self.session_path = None if state_trial else session_path
        self.session_error = None; self.session_writable = not state_trial
        self.reader = ThreadPoolExecutor(max_workers=1) if background_reads else None
        self.pending_read = None; self.loading = False; self.closed = False
        self.read_poll = None; self.title_poll = None
        self.auto_epoch = 0; self.auto_timer = None; self.auto_poll = None
        self.auto_future = None; self.auto_interval_ms = 5000
        self.auto_failed = False; self.pending_automatic = False
        root.title('Work Map — 新形式の試験閲覧' if state_trial else 'Work Map — ローカル閲覧・編集')
        root.geometry('1440x940'); root.minsize(1080, 700)
        icon_dir = Path(__file__).with_name('assets')
        self.app_icon = tk.PhotoImage(master=root, file=str(icon_dir / 'workmap.png'))
        root.iconphoto(True, self.app_icon)
        if os.name == 'nt':
            root.iconbitmap(str(icon_dir / 'workmap.ico'))
        root.configure(bg=PAPER)
        style = ttk.Style(root); style.theme_use('clam')
        style.configure('.', font=('Yu Gothic UI', 11), background=PAPER, foreground=INK)
        style.configure('TButton', padding=(14, 8), background=PAPER, borderwidth=0)
        style.map('TButton', background=[('active', '#e6e3dc')])
        style.configure('TNotebook', background=PAPER, borderwidth=0)
        style.configure('TNotebook.Tab', padding=(16, 12), background='#e9e7e0', borderwidth=0)
        style.map('TNotebook.Tab', background=[('selected', PAPER)], foreground=[('selected', ACCENT)])
        style.configure('Map.Treeview', rowheight=43, font=('Yu Gothic UI', 11),
                        background=DARK, fieldbackground=DARK, foreground='#e4e8e9', borderwidth=0)
        style.configure('Map.Treeview.Heading', background=DARK, foreground='#a8b3b9',
                        font=('Yu Gothic UI', 10), relief='flat', padding=(4, 12))
        style.map('Map.Treeview', background=[('selected', '#48555e')], foreground=[('selected', '#ffffff')])
        style.configure('Map.TEntry', fieldbackground='#394249', foreground='#ffffff', padding=8)
        page = tk.Frame(root, bg=PAPER); page.pack(fill='both', expand=True)
        top = tk.Frame(page, bg=PAPER, padx=26, pady=20); top.pack(fill='x')
        brand = tk.Frame(top, bg=PAPER); brand.pack(side='left', fill='x', expand=True)
        tk.Label(brand, text='WORK MAP   /   LOCAL MAP', bg=PAPER, fg=ACCENT,
                 font=('Segoe UI', 10, 'bold'), anchor='w').pack(fill='x')
        self.project_title = tk.Label(brand, text='仕事の全体を見渡す', bg=PAPER, fg=INK,
                                      font=('Yu Gothic UI', 20, 'bold'), anchor='w')
        self.project_title.pack(fill='x', pady=(5, 0))
        self.open_button = ttk.Button(top, text='Mapを開く', command=self.choose_store)
        self.open_button.pack(side='right', padx=(12, 0))
        self.close_button = ttk.Button(top, text='Mapを閉じる', command=self.close_current, state='disabled')
        self.close_button.pack(side='right', padx=(12, 0))
        self.edit_button = ttk.Button(top, text='選択Workを編集', command=self.edit_selected)
        self.edit_button.pack(side='right', padx=(12, 0))
        self.add_button = ttk.Button(top, text='Workを追加', command=self.add_work)
        self.add_button.pack(side='right', padx=(12, 0))
        ttk.Button(top, text='再読込  F5', command=self.refresh).pack(side='right')
        bar = tk.Frame(page, bg=PAPER, padx=26); bar.pack(fill='x', pady=(0, 14))
        self.picker = ttk.Combobox(bar, state='readonly', width=46)
        self.picker.pack(side='left', fill='x', expand=True)
        self.picker.bind('<<ComboboxSelected>>', lambda _: self.activate(self.picker.current()))
        self.status = tk.Label(bar, text='正本を選んでください', bg=PAPER, fg=MUTED,
                               font=('Yu Gothic UI', 10), padx=18, anchor='w')
        self.status.pack(side='left')
        auto_row = tk.Frame(page, bg=PAPER, padx=26); auto_row.pack(fill='x', pady=(0, 10))
        self.auto_enabled = tk.BooleanVar(master=root, value=False)
        self.auto_button = ttk.Checkbutton(auto_row, text='自動更新', variable=self.auto_enabled,
                                           command=self.toggle_auto_refresh)
        self.auto_button.pack(side='left')
        self.auto_note = tk.Label(auto_row, text='OFF · F5で再読込', bg=PAPER, fg=MUTED,
                                  font=('Yu Gothic UI', 10))
        self.auto_note.pack(side='left', padx=12)
        panes = tk.PanedWindow(page, orient='horizontal', bg='#d8d8d1', bd=0,
                               sashwidth=5, sashrelief='flat', showhandle=False)
        panes.pack(fill='both', expand=True)
        left = tk.Frame(panes, bg=DARK, padx=18, pady=14)
        right = tk.Frame(panes, bg=PAPER)
        panes.add(left, width=690, minsize=470, stretch='always'); panes.add(right, minsize=440, stretch='always')
        toolbar = tk.Frame(left, bg=DARK); toolbar.pack(fill='x', pady=(0, 12))
        tk.Label(toolbar, text='MAP', bg=DARK, fg='#a8b3b9', font=('Segoe UI', 10, 'bold')).pack(side='left')
        for text, command in [('全体へ', self.show_all), ('畳む', lambda: self.expand_tree(False)), ('展開', lambda: self.expand_tree(True))]:
            tk.Button(toolbar, text=text, command=command, bg=DARK, fg='#d4dbdf', relief='flat',
                      activebackground='#3b464e', activeforeground='#ffffff', font=('Yu Gothic UI', 10),
                      padx=10, cursor='hand2').pack(side='right')
        self.search = tk.StringVar(master=root)
        searchrow = tk.Frame(left, bg=DARK); searchrow.pack(fill='x', pady=(0, 12))
        tk.Label(searchrow, text='検索', bg=DARK, fg='#c4cdd2', font=('Yu Gothic UI', 10)).pack(side='left', padx=(0, 10))
        entry = ttk.Entry(searchrow, textvariable=self.search, style='Map.TEntry')
        entry.pack(side='left', fill='x', expand=True)
        self.search.trace_add('write', lambda *_: self.render_tree())
        self.tree = ttk.Treeview(left, columns=('people',), show='tree headings', selectmode='browse', style='Map.Treeview')
        self.tree.heading('#0', text='Work', anchor='w'); self.tree.heading('people', text='担当と状態', anchor='w')
        self.tree.column('#0', width=460, minwidth=250); self.tree.column('people', width=170, minwidth=155, stretch=False)
        y = ttk.Scrollbar(left, orient='vertical', command=self.tree.yview)
        x = ttk.Scrollbar(left, orient='horizontal', command=self.tree.xview)
        self.column_divider = tk.Frame(self.tree, bg='#85929a', width=2)
        def horizontal(first, last):
            x.set(first, last); self.update_column_divider()
        self.tree.configure(yscrollcommand=y.set, xscrollcommand=horizontal)
        y.pack(side='right', fill='y'); x.pack(side='bottom', fill='x'); self.tree.pack(fill='both', expand=True)
        self.tree.bind('<<TreeviewSelect>>', lambda _: self.select_work())
        self.tree.bind('<Double-1>', self.tree_double_click)
        self.tree.bind('<Configure>', lambda _: self.update_column_divider())
        self.tree.bind('<ButtonRelease-1>', self.tree_click, add='+')
        self.tree.bind('<Return>', lambda _: self.tabs.select(self.details))
        self.tree.tag_configure('active', foreground='#aee2c5'); self.tree.tag_configure('held', foreground='#e5c59e')
        self.tabs = ttk.Notebook(right); self.tabs.pack(fill='both', expand=True)
        self.people = self.text_tab('担当別'); self.details = self.text_tab('Work詳細')
        self.thread = self.text_tab('Workの理由')
        self.thread_search = tk.StringVar(master=root)
        self.all_thread = self.text_tab('全体の理由', search_var=self.thread_search)
        self.tabs.bind('<<NotebookTabChanged>>', lambda _: self.show_all_thread(keep_view=True))
        self.thread_search.trace_add('write', self.search_all_thread)
        foot = tk.Frame(page, bg=PAPER, padx=26); foot.pack(side='bottom', fill='x')
        self.connection = FoldSection(foot, '接続・取得情報', '')
        self.connection.pack(fill='x')
        self.location = self.connection.text
        if state_trial:
            for button in (self.open_button, self.edit_button, self.add_button):
                button.configure(state='disabled')
        else:
            ttk.Button(self.connection.body, text='理由の根拠フォルダを選ぶ', command=self.choose_evidence).pack(anchor='w', pady=(0, 8))
        footer = ('新形式の試験用Map   ·   F5で再取得' if self.trial_write else
                  '新形式の試験閲覧（読取専用）   ·   F5で再取得') if state_trial else '編集は正規の許可・競合検査を使用   ·   ▶ 作業中  ⏸ 保持中   ·   A・B…は担当（右で詳細）   ·   ？は担当の記録に未確認あり   ·   F5で再取得'
        tk.Label(page, text=footer,
                 bg=PAPER, fg=MUTED, font=('Yu Gothic UI', 9), anchor='w', padx=26, pady=8).pack(side='bottom', fill='x', before=foot)
        # Reserve the footer before giving remaining height to the scrolling panes.
        panes.pack_forget(); panes.pack(fill='both', expand=True)
        # A visible, non-modal card: the Map picker and close controls stay usable.
        self.loading_panel = tk.Frame(root, bg=DARK, padx=32, pady=24,
                                      highlightbackground='#59666e', highlightthickness=1)
        tk.Label(self.loading_panel, text='WORK MAP', bg=DARK, fg='#b4c4cc',
                 font=('Segoe UI', 10, 'bold')).pack()
        self.loading_message = tk.Label(self.loading_panel, text='Mapを読み込んでいます',
                                        bg=DARK, fg='#ffffff', font=('Yu Gothic UI', 20, 'bold'), width=26)
        # Keep one paint rectangle when the stage text changes. Shrinking and
        # recentering the native Label can leave old glyphs during screen assembly.
        self.loading_message.pack(fill='x', pady=(10, 6))
        self.loading_name = tk.Label(self.loading_panel, bg=DARK, fg='#d4dbdf',
                                    font=('Yu Gothic UI', 11), wraplength=340)
        self.loading_name.pack(pady=(0, 18))
        self.progress = ttk.Progressbar(self.loading_panel, mode='indeterminate', length=280)
        self.progress.pack()
        root.bind('<F5>', lambda _: self.refresh())
        root.bind_all('<MouseWheel>', self.scroll_details)
        root.protocol('WM_DELETE_WINDOW', self.close_window)

    def save_session(self):
        if self.session_path is None:
            return
        if self.session_writable:
            # A failed first open is not remembered as a verified project.
            sources = [s for s in self.sources if s.project_id is not None]
            selected = self.sources[self.current] if self.current >= 0 else None
            value = {'version': 1, 'maps': [
                {'path': str(s.path), 'evidence_root': str(s.evidence_root), 'project_id': s.project_id}
                for s in sources], 'selected': str(selected.path) if selected in sources else None}
            temporary = None
            try:
                self.session_path.parent.mkdir(parents=True, exist_ok=True)
                with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=self.session_path.parent,
                                                 prefix='viewer-session-', suffix='.tmp', delete=False) as stream:
                    temporary = Path(stream.name)
                    json.dump(value, stream, ensure_ascii=False, indent=2)
                os.replace(temporary, self.session_path)
                self.session_error = None
            except OSError:
                self.session_error = 'Map一覧を保存できません。今回は画面内だけで保持します。'
            finally:
                if temporary is not None:
                    try:
                        temporary.unlink(missing_ok=True)
                    except OSError:
                        pass
        if self.session_error:
            self.status.configure(text=self.session_error, foreground=ACCENT)

    def restore_session(self, preferred_source=None):
        if self.session_path is None:
            return
        try:
            sources, selected = read_session(self.session_path)
        except FileNotFoundError:
            return
        except (OSError, ValueError, TypeError):
            self.session_writable = False
            self.session_error = 'Map一覧を復元できません。保存内容は上書きせず、今回は画面内だけで保持します。'
            self.status.configure(text=self.session_error, foreground=ACCENT)
            return
        if preferred_source is not None:
            project, path = preferred_source
            removed = [s for s in sources if s.project_id == project and s.path.resolve() != path.resolve()]
            if removed:
                sources = [s for s in sources if s not in removed]
                if selected is not None and any(str(s.path) == selected for s in removed): selected = None
                self.session_error = '保存場所が変わった同じMapの旧接続は開きません。必要なら保存先を確認して「Mapを開く」から選び直してください。'
                self.status.configure(text=self.session_error, foreground=ACCENT)
        if self.shared_formats:
            sources = [replace(s, state_trial=None, require_current=True, state_only=self.state_only) for s in sources]
        self.sources = sources
        self.picker['values'] = [s.path.name for s in sources]
        if self.reader is None:
            self.show_titles(_read_titles(sources))
        elif sources:
            future = self.reader.submit(_read_titles, [replace(s) for s in sources])
            self.title_poll = self.root.after(25, lambda: self.poll_titles(future))
        if selected is not None:
            self.activate(next(i for i, s in enumerate(sources) if str(s.path) == selected))

    def poll_titles(self, future):
        if self.closed:
            return
        if not future.done():
            self.title_poll = self.root.after(25, lambda: self.poll_titles(future)); return
        self.show_titles(future.result())

    def show_titles(self, titles):
        for path, project, title in titles:
            if title and any(s.path == path and s.project_id == project for s in self.sources):
                # A fully loaded snapshot is stronger than this menu-only read.
                if not (self.data and self.current >= 0 and self.sources[self.current].path == path):
                    self.project_names[str(path)] = title
        self.picker['values'] = [f"{i + 1:02d}  {self.project_names.get(str(s.path), s.path.name)}"
                                 for i, s in enumerate(self.sources)]
        if self.current >= 0:
            self.picker.current(self.current)

    def end_loading(self):
        self.loading = False
        self.pending_automatic = False
        self.progress.stop(); self.loading_panel.place_forget()
        self.root.configure(cursor='')

    def editor_open(self):
        editor = getattr(self, 'editor', None)
        return editor is not None and bool(editor.winfo_exists())

    def cancel_auto_check(self):
        for callback in (self.auto_timer, self.auto_poll):
            if callback is not None:
                self.root.after_cancel(callback)
        self.auto_timer = self.auto_poll = None
        if self.auto_future is not None:
            self.auto_future.cancel(); self.auto_future = None

    def schedule_auto(self, delay=None):
        if self.auto_timer is not None:
            self.root.after_cancel(self.auto_timer); self.auto_timer = None
        if not self.closed and self.auto_enabled.get() and self.current >= 0:
            self.auto_timer = self.root.after(self.auto_interval_ms if delay is None else delay, self.check_auto)

    def toggle_auto_refresh(self):
        self.auto_epoch += 1; self.cancel_auto_check()
        if not self.auto_enabled.get():
            if self.pending_automatic:
                self.request_number += 1
                if self.read_poll is not None:
                    self.root.after_cancel(self.read_poll); self.read_poll = None
                if self.pending_read is not None:
                    self.pending_read.cancel()
                self.end_loading()
            self.auto_note.configure(text='OFF · F5で再読込', fg=MUTED)
            return
        if self.reader is None:
            self.reader = ThreadPoolExecutor(max_workers=1)
        self.auto_note.configure(text='ON · 5秒ごとに版の変更を確認', fg=MUTED)
        self.schedule_auto(0)

    def check_auto(self):
        self.auto_timer = None
        if self.closed or not self.auto_enabled.get() or self.current < 0:
            return
        if self.loading or self.editor_open():
            self.auto_note.configure(text='編集中は自動更新を一時停止' if self.editor_open() else '読込み完了を待っています', fg=MUTED)
            self.schedule_auto(); return
        if self.data is None:
            self.refresh(automatic=True); return
        source = self.sources[self.current]; number = self.request_number; epoch = self.auto_epoch
        self.auto_future = self.reader.submit(read_marker, replace(source), self.data['selection_paths'])
        self.auto_poll = self.root.after(25, lambda future=self.auto_future: self.poll_auto(future, number, epoch, source))

    def poll_auto(self, future, number, epoch, source):
        self.auto_poll = None
        if (self.closed or not self.auto_enabled.get() or epoch != self.auto_epoch
                or number != self.request_number or self.current < 0 or self.sources[self.current] is not source):
            return
        if not future.done():
            self.auto_poll = self.root.after(25, lambda: self.poll_auto(future, number, epoch, source)); return
        self.auto_future = None
        if self.editor_open():
            self.auto_note.configure(text='編集中は自動更新を一時停止', fg=MUTED)
            self.schedule_auto(); return
        try:
            marker = future.result()
        except (OSError, ValueError, StoreError) as exc:
            self.auto_error(exc); self.schedule_auto(); return
        if (self.auto_failed or self.data['thread']['status'] == 'unknown'
                or marker != self.data['refresh_marker']):
            self.refresh(automatic=True)
        else:
            self.auto_note.configure(text='版の変化なし · 確認 ' + datetime.now().astimezone().strftime('%H:%M:%S'), fg=MUTED)
            self.schedule_auto()

    def auto_error(self, exc):
        self.auto_failed = True
        stamp = self.data['read_at'][11:19] if self.data else '未取得'
        self.status.configure(text='未確認：自動更新に失敗 · 前回取得 ' + stamp, foreground='#a02e2e')
        self.auto_note.configure(text='再確認します · ' + str(exc)[:100], fg='#a02e2e')

    def close_window(self):
        if self.closed:
            return
        self.closed = True; self.request_number += 1
        self.cancel_auto_check()
        for callback in (self.read_poll, self.title_poll):
            if callback is not None:
                self.root.after_cancel(callback)
        self.read_poll = None; self.title_poll = None
        if self.reader is not None:
            self.reader.shutdown(wait=False, cancel_futures=True)
        self.save_session()
        self.root.destroy()

    def close_current(self):
        if self.current < 0:
            return
        if self.read_poll is not None:
            self.root.after_cancel(self.read_poll); self.read_poll = None
        self.cancel_auto_check()
        source = self.sources.pop(self.current)
        if self.pending_read is not None:
            self.pending_read.cancel()
        self.end_loading()
        self.project_names.pop(str(source.path), None)
        self.request_number += 1; self.focus_key = None; self.open_nodes = {}
        self.clear(); self.search.set(''); self.thread_search.set('')
        self.picker['values'] = [self.project_names.get(str(s.path), s.path.name) for s in self.sources]
        if self.sources:
            self.activate(min(self.current, len(self.sources) - 1))
        else:
            self.current = -1; self.picker.set('')
            self.project_title.configure(text='仕事の全体を見渡す')
            self.put(self.location, '')
            self.status.configure(text='Mapは開いていません', foreground=MUTED)
            self.close_button.configure(state='disabled')
            self.save_session()
            self.auto_note.configure(text='Mapを選ぶと自動更新を再開' if self.auto_enabled.get() else 'OFF · F5で再読込', fg=MUTED)

    def update_column_divider(self):
        width = sum(self.tree.column(c, 'width') for c in ('#0', 'people'))
        xpos = self.tree.column('#0', 'width') - int(self.tree.xview()[0] * width)
        if 0 < xpos < self.tree.winfo_width():
            self.column_divider.place(x=xpos, y=0, relheight=1); self.column_divider.lift()
        else:
            self.column_divider.place_forget()

    def edit_selected(self):
        selection = self.tree.selection()
        if not self.editing_available() or not selection:
            return
        from pma.workmap_editor_ui import EditDialog
        self.editor = EditDialog(self, selection[0])
        return self.editor

    def tree_click(self, event):
        self.update_column_divider()
        if self.tree.identify_row(event.y):
            self.tabs.select(self.people if self.tree.identify_column(event.x) == '#1' else self.details)

    def show_work(self, key):
        if not self.data or key not in self.data['snapshot']['map']['members']:
            return
        self.focus_key = None; self.search.set(''); self.render_tree(key)
        parent = self.tree.parent(key)
        while parent:
            self.tree.item(parent, open=True); self.open_nodes[parent] = True; parent = self.tree.parent(parent)
        self.tree.see(key); self.tabs.select(self.details)

    def display_actor_names(self):
        names = self.resolved_actor_names | self.actor_names
        return {a: f'{code}  ·  {actor_label(a, names)}' for a, code in self.actor_codes.items()}

    def render_people(self):
        pane = self.people; pane.clear(); s = self.data['snapshot']
        assignments = actor_assignments(s)
        pane.heading('誰が、何を引き受けているか', '現在のMap全体')
        pane.notice('検索や枝の折畳みにかかわらず、このMapに記録された担当を表示します。件数は作業量や余力を表しません。')
        if any(detail_context(s, k)['pin_status'] != 'confirmed' for k in s['map']['members']):
            pane.notice('担当の記録に未確認のWorkがあります。未記録の担当や、このMap外の仕事は含みません。', warning=True)
        if not assignments:
            pane.notice('現在の担当の記録はありません。'); return
        self.assignment_buttons = {}
        for actor, modes in assignments.items():
            label = self.display_actor_names()[actor]
            section = pane.section(actor, label, '', expanded=True, meta='担当ID：' + actor)
            section.text.pack_forget()
            for mode, title in [('active','▶ 作業中'), ('held','⏸ 保持中')]:
                works = modes[mode]
                tk.Label(section.body, text=f'{title}  {len(works)}件', bg=PAPER, fg=INK,
                         font=('Yu Gothic UI', 11, 'bold'), anchor='w').pack(fill='x', pady=(10, 4))
                pane.contents.append(title + '\n' + '\n'.join(s['display_ids'][k] + ' ' + s['works'][k]['name'] for k in works))
                for key in works:
                    button = tk.Button(section.body, text=s['display_ids'][key] + '  ' + s['works'][key]['name'],
                                       command=lambda k=key: self.show_work(k), bg=PAPER, fg=INK, activebackground='#e6e3dc',
                                       relief='flat', anchor='w', justify='left', font=('Yu Gothic UI',11), cursor='hand2',
                                       padx=10, pady=7, takefocus=True)
                    button.pack(fill='x')
                    button.bind('<Configure>', lambda e: e.widget.configure(wraplength=max(100,e.width-22)))
                    self.assignment_buttons[(actor,mode,key)] = button
            if section.meta:
                section.meta.pack_forget(); section.meta.pack(fill='x', pady=(8, 12))

    def scroll_details(self, event):
        widget = event.widget
        while widget is not None:
            if isinstance(widget, SectionPane):
                widget.canvas.yview_scroll(-1 if event.delta > 0 else 1, 'units')
                return 'break'
            widget = getattr(widget, 'master', None)

    def tree_double_click(self, event):
        if self.tree.identify('element', event.x, event.y) not in ('Treeitem.indicator', 'indicator'):
            self.focus_selected()

    def expand_tree(self, opened):
        def walk(parent=''):
            for item in self.tree.get_children(parent):
                self.tree.item(item, open=opened); self.open_nodes[item] = opened; walk(item)
        walk()

    def text_tab(self, title, search_var=None):
        pane = SectionPane(self.tabs, search_var=search_var); self.tabs.add(pane, text=title); return pane

    @staticmethod
    def put(widget, content):
        if isinstance(widget, SectionPane):
            widget.set_text(content)
        else:
            widget.configure(state='normal'); widget.delete('1.0', 'end'); widget.insert('1.0', content); widget.configure(state='disabled')

    def clear(self):
        if self.state_trial or self.shared_formats:
            for button in (self.add_button, self.edit_button):
                button.configure(state='disabled')
        self.data = None; self.last_detail = None
        self.all_thread_dirty = True
        self.tree.delete(*self.tree.get_children())
        self.assignment_buttons = {}
        for widget in (self.people, self.details, self.thread, self.all_thread):
            self.put(widget, '')

    def choose_store(self):
        path = filedialog.askopenfilename(parent=self.root, title='現在のSQLite正本を選ぶ', filetypes=[('SQLite', '*.sqlite3 *.db'), ('すべて', '*')])
        if path:
            self.add_source(Path(path))

    def add_source(self, path: Path, evidence_root: Path | None = None):
        path = path.resolve()
        for i, source in enumerate(self.sources):
            if source.path == path:
                if evidence_root is not None:
                    source.evidence_root = evidence_root.resolve()
                self.activate(i); return
        default_root = path.parent
        if self.shared_formats and path.name == 'workmap.sqlite3' and default_root.name == 'data':
            default_root = default_root.parent
        self.sources.append(OpenMap(path, (evidence_root or default_root).resolve(),
                                    state_trial=None if self.shared_formats else self.state_trial,
                                    require_current=self.shared_formats, state_only=self.state_only))
        self.picker['values'] = [str(s.path) for s in self.sources]
        self.activate(len(self.sources) - 1)

    def activate(self, index):
        if not 0 <= index < len(self.sources):
            return
        self.current = index; self.picker.current(index); self.focus_key = None; self.open_nodes = {}; self.clear(); self.search.set(''); self.thread_search.set('')
        self.refresh()
        self.close_button.configure(state='normal')
        self.save_session()

    def choose_evidence(self):
        if self.current < 0:
            return
        path = filedialog.askdirectory(parent=self.root, title='この正本の根拠資料ルート（docsの親）を選ぶ')
        if path:
            self.sources[self.current].evidence_root = Path(path).resolve(); self.refresh(); self.save_session()

    def refresh(self, automatic=False, select_key=None):
        if self.current < 0:
            return
        self.cancel_auto_check()
        if self.read_poll is not None:
            self.root.after_cancel(self.read_poll); self.read_poll = None
        # Invalidate the old display before I/O. Switching or failing never
        # leaves the previous project's data labelled as the selected Map.
        selected = self.tree.selection(); selected = selected[0] if selected else None
        if select_key is not None:
            selected = select_key; self.focus_key = None; self.search.set('')
        def remember(parent=''):
            for item in self.tree.get_children(parent):
                self.open_nodes[item] = bool(self.tree.item(item, 'open')); remember(item)
        remember()
        if not automatic:
            self.clear()
        self.request_number += 1; number = self.request_number
        source = self.sources[self.current]
        self.project_title.configure(text=self.project_names.get(str(source.path), source.path.name))
        self.loading = True; self.pending_automatic = automatic
        if automatic:
            self.auto_note.configure(text='変更を取得しています…', fg=MUTED)
        else:
            location = f'試験用DB：{source.path}' if self.state_trial else f'正本：{source.path}\n理由の根拠：{source.evidence_root}'
            self.put(self.location, location)
            self.status.configure(text='読込み中…', foreground='#80551a')
            self.root.configure(cursor='watch')
            self.loading_message.configure(text='Mapを読み込んでいます')
            self.loading_name.configure(text=self.project_title.cget('text'))
            self.loading_panel.place(relx=.5, rely=.5, anchor='center'); self.loading_panel.lift()
            self.progress.stop(); self.progress.start(12)
        self.root.update_idletasks()
        if self.reader is not None:
            if self.pending_read is not None:
                self.pending_read.cancel()
            # Each job opens/closes its own read-only connection; no Tk work
            # or canonical update runs on the worker thread.
            self.pending_read = self.reader.submit(load_map, replace(source))
            self.read_poll = self.root.after(25, lambda future=self.pending_read: self.poll_map(future, number, source, selected, automatic))
        else:
            self.finish_read(number, source, selected, automatic=automatic)

    def poll_map(self, future, number, source, selected, automatic=False):
        if self.closed or number != self.request_number:
            return
        if not future.done():
            self.read_poll = self.root.after(25, lambda: self.poll_map(future, number, source, selected, automatic)); return
        self.finish_read(number, source, selected, future, automatic)

    def finish_read(self, number, source, selected, future=None, automatic=False):
        if self.closed or number != self.request_number or self.current < 0 or self.sources[self.current] is not source:
            return
        if automatic and (not self.auto_enabled.get() or self.editor_open()):
            self.end_loading(); self.schedule_auto(); return
        try:
            data = load_map(source) if future is None else future.result()
        except (OSError, ValueError, StoreError) as exc:
            self.end_loading()
            if automatic:
                self.auto_error(exc); self.schedule_auto(); return
            self.status.configure(text='未確認：読み込めません', foreground='#a02e2e')
            self.put(self.details, str(exc) + '\n\n保存先・権限を確認して再読込してください。復旧やDB作成はこの画面では行いません。')
            self.save_session()
            self.schedule_auto()
            return
        if not automatic:
            self.status.configure(text='表示を準備しています…', foreground='#80551a')
            self.loading_message.configure(text='表示を準備しています')
            self.root.update_idletasks()
        old_data = self.data
        if automatic:
            # The user may have selected a different Work while the worker read.
            current_selection = self.tree.selection()
            selected = current_selection[0] if current_selection else None
        saved_panes = {pane: pane_state(pane) for pane in (self.people, self.details, self.thread)} if automatic else {}
        tree_view = self.tree.yview()
        first_identity = source.project_id is None
        self.data = data; s = data['snapshot']; source.project_id = s['project_id']
        source.state_trial = s.get('format_version') == 'workmap-state-1'
        if source.state_trial:
            source.map_id = s['map_id']
        resolved_names = local_actor_names(self.task_index_path, actor_assignments(s))
        names_changed = resolved_names != self.resolved_actor_names
        self.resolved_actor_names = resolved_names
        for actor in actor_assignments(s):
            if actor not in self.actor_codes:
                self.actor_codes[actor] = actor_code(len(self.actor_codes))
        if names_changed or not old_data or map_inputs(old_data['snapshot'], people=True) != map_inputs(s, people=True):
            self.render_people()
        children = {r['child'] for r in s['map']['relations'] if r['kind'] == 'parent'}
        roots = [s['works'][k]['name'] for k in s['map']['members'] if k not in children]
        self.project_names[str(source.path)] = ' / '.join(roots) or s['project_id']
        self.project_title.configure(text=self.project_names[str(source.path)])
        self.loading_name.configure(text=self.project_names[str(source.path)])
        self.picker['values'] = [f"{i + 1:02d}  {self.project_names.get(str(item.path), item.path.name)}" for i, item in enumerate(self.sources)]
        self.picker.current(self.current)
        location = f'試験用DB：{source.path}' if self.state_trial else f'正本：{source.path}\n理由の根拠：{source.evidence_root}'
        self.put(self.location, f"{location}\nプロジェクト：{s['project_id']}\n版：{s['revision']}\n取得：{data['read_at']}\n表示は本文を取得した時点の情報です。自動確認は版とファイル属性の確認で、本文の再検証とは区別します。")
        self.all_thread_dirty = (
            self.all_thread_dirty or not old_data
            or thread_inputs(old_data, thread_work_labels(old_data)) != thread_inputs(data, thread_work_labels(data))
        )
        self.show_all_thread(keep_view=automatic)
        if self.focus_key not in s['map']['members']:
            self.focus_key = None
        self.last_detail = None
        if not old_data or map_inputs(old_data['snapshot']) != map_inputs(s):
            self.render_tree(selected)
        else:
            self.select_work()
        for pane, state in saved_panes.items():
            restore_folds(pane, state)
        if saved_panes:
            self.root.update_idletasks()
            for pane, state in saved_panes.items():
                pane.canvas.yview_moveto(state[1])
            if tree_view:
                self.tree.yview_moveto(tree_view[0])
        self.end_loading()
        self.auto_failed = False
        status = f"{len(s['map']['members'])} WORKS  /  {data['read_at'][11:19]} 取得"
        writable = self.editing_available(loading_ok=True)
        if self.shared_formats or source.state_trial:
            for button in (self.add_button, self.edit_button):
                button.configure(state='normal' if writable else 'disabled')
        if source.state_trial and self.state_trial:
            role = '復旧候補' if s['metadata']['store_role'] == 'recovery_candidate' else '試験Map'
            status = f'{role}{"・試験編集" if writable else "・読取専用"}  /  {status}'
        self.status.configure(text=status, foreground=MUTED)
        if data['thread']['status'] != 'confirmed':
            self.status.configure(text=self.status.cget('text') + '  /  理由に未確認あり', foreground=ACCENT)
        if self.auto_enabled.get():
            self.auto_note.configure(text='ON · 5秒ごとに版の変更を確認', fg=MUTED)
        if not automatic or first_identity:
            self.save_session()
        self.schedule_auto()

    def add_work(self):
        if not self.editing_available():
            return
        if self.editor_open():
            self.editor.lift(); return self.editor
        from pma.workmap_add_ui import AddWorkDialog
        selected = self.tree.selection()
        self.editor = AddWorkDialog(self, selected[0] if selected else None)
        return self.editor

    def editing_available(self, *, loading_ok=False):
        if self.data is None or self.loading and not loading_ok:
            return False
        source = self.sources[self.current]
        return (not source.state_trial or (self.trial_write or self.shared_formats)
                and self.data['snapshot']['metadata']['store_role'] == 'canonical_current')

    def show_all(self):
        self.focus_key = None; self.search.set(''); self.render_tree()

    def focus_selected(self):
        selected = self.tree.selection()
        if selected:
            self.focus_key = selected[0]; self.render_tree(self.focus_key)

    def render_tree(self, selected=None):
        if self.data is None:
            return
        s = self.data['snapshot']; members = set(s['map']['members'])
        parents = {r['child']: r['parent'] for r in s['map']['relations'] if r['kind'] == 'parent'}
        children = {}
        for k in members:
            children.setdefault(parents.get(k), []).append(k)
        allowed = set(members)
        if self.focus_key:
            allowed = set(); pending = [self.focus_key]
            while pending:
                k = pending.pop(); allowed.add(k); pending.extend(children.get(k, []))
        query = self.search.get().strip().casefold()
        if query:
            matches = {k for k in allowed if query in (s['display_ids'][k] + ' ' + s['works'][k]['name'] + ' ' + (s['works'][k]['intent'] or '')).casefold()}
            visible = set(matches)
            for k in matches:
                while parents.get(k) in allowed:
                    k = parents[k]; visible.add(k)
        else:
            visible = allowed
        def remember(parent=''):
            for item in self.tree.get_children(parent):
                self.open_nodes[item] = bool(self.tree.item(item, 'open')); remember(item)
        remember()
        self.tree.delete(*self.tree.get_children())
        order = lambda k: tuple(int(p) for p in s['display_ids'][k][1:].split('.'))
        def insert(k, parent=''):
            pins = [p for p in s['map']['pins'] if p['work_id'] == k]
            modes = [p['mode'] for p in pins]
            text = pin_badges(pins, self.actor_codes)
            context = detail_context(s, k)
            if context['pin_status'] != 'confirmed':
                text += ' ？'
            self.tree.insert(parent, 'end', iid=k, text=s['display_ids'][k] + '  ' + s['works'][k]['name'],
                             values=(text or '—',), open=self.open_nodes.get(k, parent == '' or parents.get(parent) is None), tags=tuple(modes))
            for child in sorted(children.get(k, []), key=order):
                if child in visible:
                    insert(child, k)
        for k in sorted((k for k in visible if parents.get(k) not in visible), key=order):
            insert(k)
        if selected in visible:
            self.tree.selection_set(selected)
            parent = self.tree.parent(selected); opened = True
            while parent:
                opened = opened and bool(self.tree.item(parent, 'open')); parent = self.tree.parent(parent)
            if opened:
                self.tree.see(selected)
        elif self.tree.get_children():
            self.tree.selection_set(self.tree.get_children()[0])
        else:
            self.last_detail = None
            self.put(self.details, '該当するWorkはありません。'); self.put(self.thread, '')
        self.select_work()

    def search_all_thread(self, *_):
        self.all_thread_dirty = True
        self.show_all_thread()

    def show_all_thread(self, *, keep_view=False):
        if not self.all_thread_dirty or self.data is None or self.tabs.select() != str(self.all_thread):
            return
        saved_view = pane_state(self.all_thread) if keep_view else None
        self.render_thread(self.all_thread)
        self.all_thread_dirty = False
        if saved_view is not None:
            restore_folds(self.all_thread, saved_view)
            if saved_view[1]:
                self.root.update_idletasks()
                self.all_thread.canvas.yview_moveto(saved_view[1])

    def render_thread(self, pane, key=None):
        pane.clear(); data = self.data; s = data['snapshot']; selection = data['thread']
        pane.heading('判断の理由', 'WORK THREAD' if key else 'PROJECT THREAD')
        if selection['status'] == 'unknown':
            pane.notice('理由は未確認です。根拠資料を読み込めていません。', warning=True)
            pane.section('error', '確認できない理由', selection.get('reason', '') + '\n接続・取得情報から根拠フォルダを選んでください。', expanded=True)
            return
        if selection['status'] == 'needs_review':
            pane.notice('選別待ちの記録があります。通常の理由表示には混ぜていません。', warning=True)
        query = self.thread_search.get() if key is None else ''
        entries = thread_entries(data, key, query)
        labels = thread_work_labels(data)
        if query.strip():
            pane.notice(f'終了済みWorkを含む理由の検索：{len(entries)}件')
        if not entries:
            pane.notice('検索に一致する理由はありません。' if query.strip() else 'この範囲の意味Threadはありません。'); return
        raw = {e['id']: e for e in s['thread_entries']}
        for i, e in enumerate(entries):
            names = ' / '.join(labels.get(k, {'label': k})['label'] for k in e['works'])
            content = e['why']
            for field in ('identity', 'outcome', 'successor', 'residuals', 'consequence', 'revisit'):
                if field in e:
                    content += '\n\n' + LABELS.get(field, field) + '\n' + describe(e[field])
            if e.get('original_reasons'):
                content += '\n\n判断理由の原文\n' + describe(e['original_reasons'])
            if e.get('historical'):
                content += '\n\n判断時の補足\n' + describe(e['historical'])
            related = e.get('related_entries', [])
            state_trial = s.get('format_version') == 'workmap-state-1'
            refs = [e['id'], *related] if state_trial else related
            provenance = [raw[k] for k in dict.fromkeys(refs) if k in raw]
            meta = '根拠\n' + describe(e['source_refs']) + '\n\n関連する記録のID\n' + describe(related)
            if state_trial:
                from pma.workmap_state_read import trial_source_locations
                locations = trial_source_locations(self.sources[self.current].path, [e])
                meta += '\n\n保存された根拠の場所（内容の照合は別）\n' + describe(locations)
                if s['metadata'].get('migration') is not None and any(v is None for v in locations.values()):
                    meta += '\n確認できない版は参照先を表示しています。'
            meta += '\n\n参照する記録（ID：出来事の日時 ／ 記録日時）\n'
            meta += '\n'.join(r['id'] + '：' + describe(r.get('occurred_at')) + ' ／ ' + describe(r.get('recorded_at'))
                               + '\n記録した担当：' + describe(r.get('actor')) for r in provenance)
            pane.section(e['id'], names + '   ' + e['summary'], content, expanded=key is not None and i == 0, meta=meta)

    def select_work(self):
        selection = self.tree.selection()
        if not self.data or not selection:
            return
        key = selection[0]
        if self.last_detail == key:
            return
        self.last_detail = key
        s = self.data['snapshot']; work = s['works'][key]; context = detail_context(s, key)
        pane = self.details; pane.clear()
        pane.heading(work['name'], s['display_ids'][key])
        pane.section('purpose', '目的', describe(work['intent']), expanded=True, warning=work['intent'] is None)
        pins = [p for p in s['map']['pins'] if p['work_id'] == key]
        people = people_groups(pins, self.display_actor_names())
        unknown = context['pin_status'] != 'confirmed'
        if unknown:
            people += '\n\n関与の完全性は未確認です。上記は正本に記録された担当だけです。'
        pane.section('people', '担当と現在地' + ('  ·  未確認あり' if unknown else ''), people, expanded=True, warning=unknown)
        pane.section('origin', '生まれた理由', describe(work['origin'].get('summary')), meta=describe(work['origin'].get('refs')))
        for name, status_field, label in [('action_rules','action_rule_status','行動ルール'), ('permissions','permission_status','許可の範囲')]:
            records = [r for r in s['map'][name] if key in r['targets']]
            unknown_authority = False
            unknown = context[status_field] != 'confirmed'
            if name == 'permissions':
                chain_keys = {w['work_key'] for w in context['parent_chain']}
                # Display declarations too, so a copied Map explains why its
                # saved Map permission is now inapplicable instead of hiding it.
                map_grants = [r for r in s['map']['permissions']
                              if MAP_ADD_OPERATION in r['operations'] and chain_keys.intersection(r['targets'])]
                records += [r for r in map_grants if r not in records]
                if s.get('format_version') == 'workmap-state-1':
                    from pma.workmap_state_contract import permission_authority_status
                    unknown_authority = any(permission_authority_status(s,r) == 'unknown' for r in records)
                for grant in map_grants:
                    root_context = detail_context(s, grant['targets'][0])
                    unknown = unknown or any(root_context[f] != 'confirmed' for f in ('permission_status', 'action_rule_status'))
            suffix = '未確認あり' if unknown else (str(len(records)) + '件' if records else 'なし')
            content = current_record_text(records, s, name, self.resolved_actor_names | self.actor_names | self.display_actor_names(), store_path=self.sources[self.current].path)
            if unknown_authority:
                suffix = '根拠の版に未確認あり'
                content = '版が未確認の根拠に基づく許可では書き込めません。\n\n' + content
            if unknown:
                content = '集合の完全性は未確認。以下は記録された分だけです。\n\n' + content
            pane.section(name, label + '  ·  ' + suffix, content, expanded=name == 'action_rules' and (bool(records) or unknown), warning=unknown or unknown_authority or name == 'action_rules' and bool(records), meta=describe(records) if records else None)
        chain = context['parent_chain']; ancestors = {p['work_key'] for p in chain[:-1]}
        inherited = [r for name in ('permissions','action_rules') for r in s['map'][name] if ancestors.intersection(r['targets'])]
        unknown_ancestors = any(detail_context(s, k)[f] != 'confirmed' for k in ancestors for f in ('permission_status','action_rule_status'))
        pane.section('ancestors', '親側の許可・制約' + ('  ·  未確認あり' if unknown_ancestors else '  ·  適用範囲を確認'),
                     '親の許可をそのままこのWorkの許可にはしません。\n' + describe(inherited), warning=unknown_ancestors)
        relationships = ' → '.join(p['display_id'] + ' ' + p['name'] for p in chain)
        relationships += '\n\n子Work\n' + describe(context['children']) + '\n\n依存\n' + describe(context['dependencies'])
        pane.section('relations', '親・子・依存関係', relationships)
        results, hints = detail_results(s, key)
        if results:
            pane.section('results', '成果・根拠の記録', '記録時点の結果です。最新の記録だけで現在状態や他の未完了を確定しません。\n' + describe(results))
        if hints:
            pane.section('notes', '担当の過去の残し書き', '回復の補助です。現在への適用は再確認してください。\n' + describe(hints))
        migration = s['metadata'].get('migration')
        if migration is not None:
            review_notes = [note for note in migration['review_notes'] if key in note['targets']]
            if review_notes:
                pane.section('migration', '移行時の確認に使う以前のメモ',
                             '記録当時の内容です。現在の判断にはMapを確認してください。\n' + describe(review_notes))
        pane.section('technical', '識別子・記録の詳細', 'Work識別子\n' + key + '\n\n担当の元記録\n' + describe(pins) + '\n\n取得した版\n' + s['revision'])
        self.render_thread(self.thread, key)


def main(*, default_source=None):
    parser = argparse.ArgumentParser(description='Work Map local viewer and permitted editor')
    parser.add_argument('--store', type=Path, action='append', default=[], help='initial SQLite paths; repeat to open multiple Maps')
    parser.add_argument('--evidence-root', type=Path, help='evidence root for the first Map; others can be selected in the window')
    parser.add_argument('--actor', help=argparse.SUPPRESS)  # Older shortcuts still open; never label an actor as "this chat".
    parser.add_argument('--actor-name', nargs=2, action='append', default=[], metavar=('ID', 'NAME'),
                        help='verified display name for an exact actor ID; repeatable, display only, creates no Pin')
    parser.add_argument('--operator-id', default='actor:human:owner',
                        help='claimed actor ID for editing (default: local Owner); creates no Pin or Permission')
    args = parser.parse_args()
    if os.name == 'nt':
        import ctypes
        # Separate this reader's taskbar identity from the Python interpreter.
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID('Ashiba.WorkMap.Reader')
    names = dict(args.actor_name)
    names.setdefault('actor:human:owner', 'Owner')
    default_root = None
    preferred = None
    if not args.store and default_source is not None:
        project, path, default_root = default_source
        preferred = (project, path)
        args.store = [path]
    root = tk.Tk(); app = WorkMapViewer(root, actor_names=names, operator_id=args.operator_id,
                                       session_path=viewer_session_path(), background_reads=True,
                                       task_index_path=local_task_index_path(), shared_formats=True)
    app.restore_session(preferred_source=preferred)
    for i, path in enumerate(args.store):
        evidence_root = (args.evidence_root or default_root) if i == 0 else None
        existing = next((s for s in app.sources if s.path == path.resolve()), None)
        if existing is None or (preferred is not None and i == 0):
            app.add_source(path, evidence_root)
        elif evidence_root is not None:
            existing.evidence_root = evidence_root.resolve()
            if app.current >= 0 and app.sources[app.current] is existing:
                app.refresh()
            app.save_session()
    root.mainloop()


if __name__ == '__main__':
    main()
