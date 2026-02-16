import sqlite3
from collections import defaultdict, Counter
import time
from datetime import datetime
from threading import Thread
import os
from pathlib import Path

from kivy.app import App
from kivy.clock import Clock
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.gridlayout import GridLayout
from kivy.uix.label import Label
from kivy.uix.button import Button
from kivy.uix.textinput import TextInput
from kivy.uix.scrollview import ScrollView
from kivy.uix.popup import Popup
from kivy.properties import ObjectProperty



class WaterPoloRoot(BoxLayout):
    controller = ObjectProperty(None)

    def __init__(self, **kwargs):
        super().__init__(orientation='vertical', **kwargs)


class WaterPoloTrackerController:
    def __init__(self, root_widget):
        self.root_widget = root_widget

        # Data dir
        self.data_dir = self.get_app_data_dir()
        self.db_path = os.path.join(self.data_dir, "db", "waterpolo.db")
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)

        # DB & state
        self.db_conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.setup_database()

        self.stats = defaultdict(lambda: defaultdict(int))
        self.current_match_id = None
        self.current_match_code = None
        self.game_running = False
        self.auto_paused = False
        self.time_remaining = 480.0
        self.current_quarter = 1
        self.possession_team = "Home"
        self.ball_holder = None
        self.pending_defensive_event = None
        self.player_names = self.load_player_names()

        self.home_score = 0
        self.away_score = 0
        self.critical_events = []
        self.CRITICAL_EVENTS = {
            'Goal', 'P.Lost', 'E.Lost', 'Yellow', 'Red', 'Wrap', 'Timeout'
        }

        self.clock_thread = None
        self.play_btn = None
        self.pause_btn = None
        self.stats_text = None
        self.log_text = None
        self.ball_label = None
        self.clock_display = None
        self.score_display = None
        self.possession_text = None
        self.match_log_path = None
        self.possession_time = defaultdict(lambda: defaultdict(float))
        self.last_possession_tick = None

        # Names control
        self.names_required = True          # enforce before match
        self.player_names_complete = False  # becomes True after saving 26

        # Substitution / pool time
        self.in_pool = {'Home': set(), 'Away': set()}
        self.starting_lineup = {'Home': [], 'Away': []}
        self.sub_events = []
        self.pool_time = defaultdict(lambda: defaultdict(float))

        # Player buttons
        self.home_players = []
        self.away_players = []

        self.create_widgets()
        self.update_clock_display()

    # ---------------- DB / FS ----------------

    def get_app_data_dir(self):
        app_name = "WaterPoloTracker"
        if os.name == 'nt':
            appdata = os.getenv('APPDATA')
            if appdata:
                return Path(appdata) / app_name
        if os.name == 'posix':
            return Path.home() / ".local" / "share" / app_name
        return Path.cwd() / app_name

    def setup_database(self):
        self.db_conn.executescript('''
            CREATE TABLE IF NOT EXISTS matches (
                match_id INTEGER PRIMARY KEY AUTOINCREMENT,
                match_code TEXT UNIQUE,
                date TEXT, home_team TEXT, away_team TEXT, final_score TEXT
            );
            CREATE TABLE IF NOT EXISTS players (
                player_id TEXT PRIMARY KEY,
                number INTEGER,
                name TEXT,
                team TEXT
            );
            CREATE TABLE IF NOT EXISTS events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                match_id INTEGER, match_code TEXT,
                player_id TEXT, event_type TEXT,
                quarter INTEGER, time_remaining REAL,
                timestamp REAL, possession_team TEXT, ball_holder TEXT
            );
            CREATE TABLE IF NOT EXISTS player_possession (
                match_id INTEGER,
                player_id TEXT,
                quarter INTEGER,
                possession_seconds REAL,
                PRIMARY KEY (match_id, player_id, quarter)
            );
            CREATE TABLE IF NOT EXISTS player_pool_time (
                match_id INTEGER,
                player_id TEXT,
                quarter INTEGER,
                pool_seconds REAL,
                substitutions INTEGER DEFAULT 0,
                PRIMARY KEY (match_id, player_id, quarter)
            );
            CREATE TABLE IF NOT EXISTS match_substitutions (
                match_id INTEGER,
                player_id TEXT,
                quarter INTEGER,
                time_remaining REAL,
                action TEXT,
                timestamp REAL
            );
        ''')
        self.db_conn.commit()

    def load_player_names(self):
        try:
            cur = self.db_conn.cursor()
            cur.execute("SELECT player_id, name FROM players")
            rows = cur.fetchall()
            return {pid: name for pid, name in rows}
        except Exception:
            return {}

    # -------------- UI BUILD ----------------

    def create_widgets(self):
        root = self.root_widget
        root.clear_widgets()

        # Title
        title_label = Label(
            text="Water Polo Tracker v3.1",
            size_hint_y=None,
            height='40dp'
        )
        root.add_widget(title_label)

        # Top bar - FIXED button sizes (total = 1.0)
        top_bar = BoxLayout(orientation='horizontal', size_hint_y=None, height='60dp')
        self.clock_display = Label(text="8:00 [] Q1", size_hint_x=0.18)  # was 0.20
        self.score_display = Label(text="0-0", size_hint_x=0.10)

        self.play_btn = Button(text="Go", size_hint_x=0.06,      # was 0.07
                               on_press=lambda *_: self.start_clock())
        self.pause_btn = Button(text="Pause", size_hint_x=0.06,     # was 0.07
                                on_press=lambda *_: self.pause_clock(),
                                disabled=True)
        reset_btn = Button(text="Reset", size_hint_x=0.06,          # was 0.07
                           on_press=lambda *_: self.reset_quarter())
        plus_btn = Button(text="+2s", size_hint_x=0.06,          # was 0.07
                          on_press=lambda *_: self.adjust_time(2))
        minus_btn = Button(text="-2s", size_hint_x=0.06,         # was 0.07
                           on_press=lambda *_: self.adjust_time(-2))
        q_btn = Button(text="Q ^", size_hint_x=0.06,              # was 0.07
                       on_press=lambda *_: self.next_quarter())
        sub_in_btn = Button(text="Sub IN", size_hint_x=0.10,       # was 0.12, shortened text
                            on_press=lambda *_: self.set_sub_mode("IN"))
        sub_out_btn = Button(text="Sub OUT", size_hint_x=0.10,     # was 0.12, shortened text
                             on_press=lambda *_: self.set_sub_mode("OUT"))

        # ADD ALL BUTTONS IN ORDER
        top_bar.add_widget(self.clock_display)
        top_bar.add_widget(self.score_display)
        top_bar.add_widget(self.play_btn)
        top_bar.add_widget(self.pause_btn)
        top_bar.add_widget(reset_btn)
        top_bar.add_widget(plus_btn)
        top_bar.add_widget(minus_btn)
        top_bar.add_widget(q_btn)
        top_bar.add_widget(sub_in_btn)
        top_bar.add_widget(sub_out_btn)
        root.add_widget(top_bar)

                # Ball label - smaller
        self.ball_label = Label(text=" No ball", size_hint_y=None, height='24dp')  # was 30dp
        root.add_widget(self.ball_label)

        # Players row - SMALLER buttons
        players_row = BoxLayout(orientation='horizontal', size_hint_y=0.35)  # was 0.50

        home_box = BoxLayout(orientation='vertical')
        home_box.add_widget(Label(text=" Home", size_hint_y=None, height='20dp'))  # shorter label
        home_grid = GridLayout(cols=7, size_hint_y=None, height='140dp')  # was 200dp
        self.home_players = []
        for i in range(14):
            btn = Button(
                text=f"P{i+1}",
                background_color=(0.12, 0.53, 0.90, 1),
                font_size='12sp',  # smaller font
                on_press=lambda inst, idx=i: self.set_ball_holder(idx, "Home")
            )
            self.home_players.append(btn)
            home_grid.add_widget(btn)
        home_box.add_widget(home_grid)

        away_box = BoxLayout(orientation='vertical')
        away_box.add_widget(Label(text="Away", size_hint_y=None, height='20dp'))  # shorter label
        away_grid = GridLayout(cols=7, size_hint_y=None, height='140dp')  # was 200dp
        self.away_players = []
        for i in range(14):
            btn = Button(
                text=f"O{i+1}",
                background_color=(0.96, 0.49, 0.0, 1),
                font_size='12sp',  # smaller font
                on_press=lambda inst, idx=i: self.set_ball_holder(idx, "Away")
            )
            self.away_players.append(btn)
            away_grid.add_widget(btn)
        away_box.add_widget(away_grid)

        players_row.add_widget(home_box)
        players_row.add_widget(away_box)
        root.add_widget(players_row)

        # Events - normal size
        events_box = BoxLayout(orientation='vertical', size_hint_y=0.22)  # was 0.18
        events_box.add_widget(Label(text="Events", size_hint_y=None, height='20dp'))


        off_grid = GridLayout(cols=8, size_hint_y=None, height='36dp')  # was 40dp
        off_events = [
            ('Goal', 'Goal'), ('Shot', 'Shot'), ('Pen', 'Pen.Win'), ('Excl', 'Excl.Win'),
            ('Dump', 'Dump'), ('Foul', 'Foul'), ('Reversal', 'Reversal'), ('Drive', 'Drive')
        ]
        for label, name in off_events:
            off_grid.add_widget(Button(
                text=label,
                background_color=(1, 0.8, 0.5, 1),
                font_size=11,  # was 12
                on_press=lambda inst, e=name: self.event_clicked(e)
            ))
        events_box.add_widget(off_grid)

        def_grid = GridLayout(cols=9, size_hint_y=None, height='36dp')  # was 40dp
        def_events = [
            ('Block', 'Block'), ('Save', 'Save'), ('P.Lost', 'P.Lost'), ('E.Lost', 'E.Lost'),
            ('Steal', 'Intercept'), ('Red C', 'Red'), ('Yel C', 'Yellow'), ('Wrap', 'Wrap'),
            ('2 Metres', 'Offside')
        ]
        for label, name in def_events:
            def_grid.add_widget(Button(
                text=label,
                background_color=(0.73, 0.86, 0.98, 1),
                font_size=11,  # was 12
                on_press=lambda inst, e=name: self.event_clicked(e)
            ))
        events_box.add_widget(def_grid)

        game_grid = GridLayout(cols=4, size_hint_y=None, height='36dp')  # was 40dp
        game_events = [
            ('Timeout', 'Timeout'), ('Corner', 'Corner'), ('Drop Ball', 'DropBall'), ('Referee Chat', 'Ref_Chat')
        ]
        for label, name in game_events:
            game_grid.add_widget(Button(
                text=label,
                background_color=(0.88, 0.75, 0.91, 1),
                font_size=11,  # was 12
                on_press=lambda inst, e=name: self.event_clicked(e)
            ))
        events_box.add_widget(game_grid)
        root.add_widget(events_box)


        # Actions row
        action_row = BoxLayout(orientation='horizontal', size_hint_y=None, height='40dp')
        crit_btn = Button(text="Critical Log", size_hint_x=0.2,
                          on_press=lambda *_: self.show_critical_popup())
        new_match_btn = Button(text="New Match", size_hint_x=0.2,
                               on_press=lambda *_: self.new_match_dialog())
        names_btn = Button(text="Names", size_hint_x=0.2,
                           on_press=lambda *_: self.edit_names())
        report_btn = Button(text="Report", size_hint_x=0.2,
                            on_press=lambda *_: self.generate_report())
        breakdown_btn = Button(text="Player Breakdown", size_hint_x=0.2,
                               on_press=lambda *_: self.show_player_breakdown())
        action_row.add_widget(crit_btn)
        action_row.add_widget(new_match_btn)
        action_row.add_widget(names_btn)
        action_row.add_widget(report_btn)
        action_row.add_widget(breakdown_btn)
        root.add_widget(action_row)

                # SMALLER LOG area
        logs_box = BoxLayout(orientation='vertical', size_hint_y=0.12)  # explicit limit
        logs_box.add_widget(Label(text="Logs", size_hint_y=None, height='20dp'))
        
        log_scroll = ScrollView()
        self.log_text = TextInput(
            readonly=True, 
            multiline=True,
            size_hint=(1, None),
            height=400  # smaller initial height
        )
        log_scroll.add_widget(self.log_text)
        logs_box.add_widget(log_scroll)
        root.add_widget(logs_box)




        # Sub mode state
        self.sub_mode = None



    # ------------ Utility ------------

    def get_player_name(self, player_id):
        if player_id is None:
            return "No player"
        if player_id in self.player_names:
            return self.player_names[player_id]
        if isinstance(player_id, str) and player_id.startswith('H-'):
            num = player_id.replace('H-Player', '')
            return f"Home #{num}"
        if isinstance(player_id, str) and player_id.startswith('A-'):
            num = player_id.replace('A-Player', '')
            return f"Away #{num}"
        return str(player_id)

    def log_message(self, message):
        if not self.log_text:
            return
        ts = datetime.now().strftime("%H:%M:%S")
        self.log_text.text += f"[{ts}] {message}\n"
        
        # Auto-scroll to bottom - schedule after layout
        def scroll_to_bottom(dt):
            self.log_text.cursor_end = True
        Clock.schedule_once(scroll_to_bottom, 0.1)

    

    # ------------ Clock / score ------------

    def update_clock_display(self, *_):
        mins = int(self.time_remaining // 60)
        secs = int(self.time_remaining % 60)
        if self.game_running:
            status = ">"
        elif self.auto_paused:
            status = "||(auto)"
        else:
            status = "[]"
        if self.clock_display:
            self.clock_display.text = f"{mins}:{secs:02d} {status} Q{self.current_quarter}"

    def reset_scores(self):
        self.home_score = 0
        self.away_score = 0
        self.update_score_display()

    def update_score_display(self):
        if self.score_display:
            self.score_display.text = f"{self.home_score}-{self.away_score}"

    def start_clock(self):
        if not self.current_match_id:
            self.log_message("Create match first!")
            return

        self.auto_paused = False
        self.game_running = True
        if self.pause_btn:
            self.pause_btn.disabled = False
        if self.play_btn:
            self.play_btn.disabled = True
        self.log_message("Clock started/resumed")

        def loop():
            self.last_possession_tick = time.time()
            while self.game_running and self.time_remaining > 0:
                time.sleep(1)
                if not self.game_running:
                    break

                now = time.time()
                dt = now - self.last_possession_tick
                self.last_possession_tick = now

                cur = self.db_conn.cursor()
                # Pool time
                for team in ['Home', 'Away']:
                    for pid in self.in_pool[team]:
                        self.pool_time[pid][self.current_quarter] += dt
                        cur.execute("""
                            INSERT OR REPLACE INTO player_pool_time
                            (match_id, player_id, quarter, pool_seconds, substitutions)
                            VALUES (
                                ?, ?, ?,
                                COALESCE(
                                    (SELECT pool_seconds FROM player_pool_time
                                     WHERE match_id=? AND player_id=? AND quarter=?),
                                    0
                                ) + ?,
                                COALESCE(
                                    (SELECT substitutions FROM player_pool_time
                                     WHERE match_id=? AND player_id=? AND quarter=?),
                                    0
                                )
                            )
                        """, (
                            self.current_match_id, pid, self.current_quarter,
                            self.current_match_id, pid, self.current_quarter, dt,
                            self.current_match_id, pid, self.current_quarter
                        ))

                # Possession time
                if self.ball_holder:
                    self.possession_time[self.ball_holder][self.current_quarter] += dt
                    cur.execute("""
                        INSERT OR REPLACE INTO player_possession
                        (match_id, player_id, quarter, possession_seconds)
                        VALUES (
                            ?, ?, ?,
                            COALESCE(
                                (SELECT possession_seconds FROM player_possession
                                 WHERE match_id=? AND player_id=? AND quarter=?),
                                0
                            ) + ?
                        )
                    """, (
                        self.current_match_id, self.ball_holder, self.current_quarter,
                        self.current_match_id, self.ball_holder, self.current_quarter, dt
                    ))

                self.db_conn.commit()

                self.time_remaining -= 1
                Clock.schedule_once(self.update_clock_display)
                Clock.schedule_once(self.update_possession_display)

            if self.time_remaining <= 0:
                self.time_remaining = 0
                self.game_running = False
                self.auto_paused = True
                Clock.schedule_once(self.update_clock_display)
                Clock.schedule_once(lambda dt: self.generate_quarter_report())
                Clock.schedule_once(self._end_of_quarter_actions)

        self.clock_thread = Thread(target=loop, daemon=True)
        self.clock_thread.start()

    def _end_of_quarter_actions(self, *_):
        if self.pause_btn:
            self.pause_btn.disabled = True
        if self.play_btn:
            self.play_btn.disabled = False
        self.log_message(f" End of Q{self.current_quarter}")

        if self.current_quarter < 4:
            self.current_quarter += 1
            self.time_remaining = 480
            self.update_clock_display()
            self.log_message(f"Ready for Q{self.current_quarter} (press play)")
            for pid in self.pool_time:
                if self.current_quarter not in self.pool_time[pid]:
                    self.pool_time[pid][self.current_quarter] = 0.0
        else:
            if self.clock_display:
                self.clock_display.text = "MATCH FINISHED"
            self.log_message("Match finished")

    def pause_clock(self):
        self.game_running = False
        self.auto_paused = False
        if self.pause_btn:
            self.pause_btn.disabled = True
        if self.play_btn:
            self.play_btn.disabled = False
        self.update_clock_display()
        self.log_message(" || Manual pause")

    def reset_quarter(self):
        self.game_running = False
        self.auto_paused = False
        self.time_remaining = 480
        self.current_quarter = 1
        if self.pause_btn:
            self.pause_btn.disabled = True
        if self.play_btn:
            self.play_btn.disabled = False
        self.update_clock_display()

    def adjust_time(self, seconds):
        self.time_remaining = max(0, self.time_remaining + seconds)
        self.game_running = False
        self.auto_paused = False
        if self.pause_btn:
            self.pause_btn.disabled = True
        if self.play_btn:
            self.play_btn.disabled = False
        self.update_clock_display()
        self.log_message(
            f"Time adjusted: {seconds:+d}s → "
            f"{int(self.time_remaining//60)}:{int(self.time_remaining%60):02d}"
        )

    def next_quarter(self):
        if self.current_quarter < 4:
            self.current_quarter += 1
        else:
            self.current_quarter = 1

        cur = self.db_conn.cursor()
        all_players = set(self.starting_lineup['Home'] + self.starting_lineup['Away'])
        for pid in all_players:
            cur.execute(
                "INSERT OR IGNORE INTO player_pool_time "
                "(match_id, player_id, quarter, pool_seconds, substitutions) "
                "VALUES (?, ?, ?, ?, ?)",
                (self.current_match_id, pid, self.current_quarter, 0.0, 0)
            )
            cur.execute(
                "INSERT OR IGNORE INTO player_possession "
                "(match_id, player_id, quarter, possession_seconds) "
                "VALUES (?, ?, ?, ?)",
                (self.current_match_id, pid, self.current_quarter, 0.0)
            )
        self.db_conn.commit()

        self.game_running = False
        self.auto_paused = False
        if self.pause_btn:
            self.pause_btn.disabled = True
        if self.play_btn:
            self.play_btn.disabled = False
        self.update_clock_display()
        self.log_message(f"Quarter → Q{self.current_quarter}")

    # ------------ Ball, subs, possession ------------

    def set_ball_holder(self, idx, team):
        player_id = f"{'H' if team == 'Home' else 'A'}-Player{idx+1}"

        if self.sub_mode:
            self.handle_substitution(player_id, team)
            return

        if self.pending_defensive_event:
            self.log_event(player_id, self.pending_defensive_event)
            self.log_message(
                f"DEF {self.get_player_name(player_id)} - "
                f"{self.pending_defensive_event} (Def) Q{self.current_quarter}"
            )
            self.update_stats_display()
            self.pending_defensive_event = None
            if self.ball_label:
                self.ball_label.text = "No ball"
            return

        self.ball_holder = player_id
        self.possession_team = team
        if self.ball_label:
            self.ball_label.text = f"{self.get_player_name(player_id)}"
        self.log_message(f"Ball → {self.get_player_name(player_id)}")

    def set_sub_mode(self, mode):
        self.sub_mode = mode
        if not self.ball_label:
            return
        if mode == "IN":
            self.ball_label.text = "Click player to SUB IN"
        else:
            self.ball_label.text = "Click player to SUB OUT"

    def log_sub_event(self, player_id, action):
        data = {
            'player': player_id,
            'quarter': self.current_quarter,
            'time_remaining': self.time_remaining,
            'action': action,
            'timestamp': time.time()
        }
        self.sub_events.append(data)
        self.db_conn.execute("""
            INSERT INTO match_substitutions
            (match_id, player_id, quarter, time_remaining, action, timestamp)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            self.current_match_id, player_id, self.current_quarter,
            self.time_remaining, action, data['timestamp']
        ))
        self.db_conn.execute("""
            UPDATE player_pool_time
            SET substitutions = substitutions + 1
            WHERE match_id = ? AND player_id = ? AND quarter = ?
        """, (self.current_match_id, player_id, self.current_quarter))
        self.db_conn.commit()

    def handle_substitution(self, player_id, team):
        if self.sub_mode == "IN":
            if player_id in self.in_pool[team]:
                self.log_message(f" {self.get_player_name(player_id)} already in pool")
            else:
                self.log_sub_event(player_id, "IN")
                self.in_pool[team].add(player_id)
                self.log_message(
                    f" {self.get_player_name(player_id)} SUB IN (Q{self.current_quarter})"
                )

        elif self.sub_mode == "OUT":
            if player_id not in self.in_pool[team]:
                self.log_message(f" {self.get_player_name(player_id)} not in pool")
            else:
                self.log_sub_event(player_id, "OUT")
                self.in_pool[team].remove(player_id)
                if self.ball_holder == player_id:
                    others = [p for p in self.in_pool[team] if p != player_id]
                    if others:
                        self.ball_holder = others[0]
                        if self.ball_label:
                            self.ball_label.text = f"Ball {self.get_player_name(self.ball_holder)}"
                        self.log_message(
                            f"> Ball auto-passed to {self.get_player_name(self.ball_holder)}"
                        )
                    else:
                        self.ball_holder = None
                        if self.ball_label:
                            self.ball_label.text = "No ball"
                        self.log_message("→ Ball cleared (no teammates)")

        self.sub_mode = None
        if self.ball_label:
            if not self.ball_holder:
                self.ball_label.text = "No ball"
            else:
                self.ball_label.text = f" {self.get_player_name(self.ball_holder)}"
        self.update_player_visuals()
        self.log_message(" Sub complete")

    def update_player_visuals(self):
        for i, btn in enumerate(self.home_players):
            pid = f"H-Player{i+1}"
            btn.background_color = (0.10, 0.46, 0.82, 1) if pid in self.in_pool['Home'] \
                                   else (0.12, 0.53, 0.90, 1)
        for i, btn in enumerate(self.away_players):
            pid = f"A-Player{i+1}"
            btn.background_color = (0.94, 0.42, 0.0, 1) if pid in self.in_pool['Away'] \
                                   else (0.96, 0.49, 0.0, 1)

    def update_possession_display(self, *_):
        if not self.possession_text:
            return
        lines = [f"Pool Time Q{self.current_quarter}:"]
        current = []
        for pid, quarters in self.pool_time.items():
            if self.current_quarter in quarters:
                subs = sum(
                    1 for s in self.sub_events
                    if s['player'] == pid and s['quarter'] == self.current_quarter
                )
                current.append((pid, quarters[self.current_quarter], subs))
        for pid, secs, subs in sorted(current, key=lambda x: x[1], reverse=True)[:8]:
            mins, rem = divmod(int(secs), 60)
            t = f"{mins}:{rem:02d}"
            name = self.get_player_name(pid)[:10]
            lines.append(f"{name:10s} {t:6s} ({subs} subs)")
        self.possession_text.text = "\n".join(lines)

    # ------------ Events & stats ------------

    def log_critical_event(self, player_id, event_type):
        mins, secs = divmod(int(self.time_remaining), 60)
        time_str = f"{mins}:{secs:02d}"
        self.critical_events.append({
            'quarter': self.current_quarter,
            'time': self.time_remaining,
            'player': player_id,
            'event': event_type,
            'time_str': time_str
        })

    def show_critical_popup(self):
        content = BoxLayout(orientation='vertical')
        content.add_widget(Label(
            text="Critical Events (Goals/P.Lost/E.Lost/Yellow/Red/Wrap/Timeout)",
            size_hint_y=None, height='30dp'
        ))
        text = TextInput(readonly=True, multiline=True)
        if self.critical_events:
            evs = sorted(self.critical_events, key=lambda x: x['time'], reverse=True)
            lines = [
                f"Q{e['quarter']} {e['time_str']}\t| {self.get_player_name(e['player'])}\t| {e['event']}"
                for e in evs
            ]
            text.text = "\n".join(lines)
        else:
            text.text = "No critical events recorded yet.\n"
        content.add_widget(text)
        btn = Button(text="Close", size_hint_y=None, height='40dp')
        content.add_widget(btn)
        popup = Popup(title=" Critical Events Log", content=content, size_hint=(0.9, 0.8))
        btn.bind(on_press=popup.dismiss)
        popup.open()

    def event_clicked(self, event_name):
        defensive = [
            'Block', 'Save', 'P.Lost', 'E.Lost',
            'Intercept', 'Red', 'Yellow', 'Wrap', 'Offside', 'Drive'
        ]
        if event_name in defensive:
            self.pending_defensive_event = event_name
            if self.ball_label:
                self.ball_label.text = f" Select defender for {event_name}"
            self.log_message(f" Waiting for defender... ({event_name})")
            return

        game_events = ['Corner', 'DropBall', 'Ref_Chat']
        if event_name in game_events:
            self.log_event("GAME", event_name)
            self.game_running = False
            self.auto_paused = True
            if self.pause_btn:
                self.pause_btn.disabled = True
            if self.play_btn:
                self.play_btn.disabled = False
            self.ball_holder = None
            if self.ball_label:
                self.ball_label.text = " No ball"
            self.update_clock_display()
            self.log_message(f" || Auto-paused: {event_name}")
            self.update_stats_display()
            return

        if not self.ball_holder:
            self.log_message("X Select player first!")
            return

        pid = self.ball_holder
        self.log_event(pid, event_name)

        auto_pause = [
            'Goal', 'Foul', 'Pen.Win', 'P.Lost', 'E.Lost', 'Red',
            'Yellow', 'Wrap', 'Excl.Win', 'Reversal', 'Timeout', 'Offside'
        ]
        if event_name in auto_pause and self.game_running:
            self.game_running = False
            self.auto_paused = True
            if self.pause_btn:
                self.pause_btn.disabled = True
            if self.play_btn:
                self.play_btn.disabled = False
            self.update_clock_display()
            self.log_message(f" || Auto-pause: {event_name}")

        if event_name == 'Goal':
            self.ball_holder = None
            if self.ball_label:
                self.ball_label.text = " No ball"

        self.log_message(
            f" {self.get_player_name(pid)} - {event_name} (Q{self.current_quarter})"
        )
        self.update_stats_display()

    def log_event(self, player_id, event_type):
        self.stats[player_id][event_type] += 1

        if event_type == 'Goal':
            if isinstance(player_id, str) and player_id.startswith('H-'):
                self.home_score += 1
            elif isinstance(player_id, str):
                self.away_score += 1
            Clock.schedule_once(lambda dt: self.update_score_display())

        if event_type in self.CRITICAL_EVENTS:
            Clock.schedule_once(lambda dt: self.log_critical_event(player_id, event_type))

        match_code = getattr(self, 'current_match_code', '')
        self.db_conn.execute("""
            INSERT INTO events
            (match_id, match_code, player_id, event_type, quarter,
             time_remaining, timestamp, possession_team, ball_holder)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            self.current_match_id, match_code, player_id, event_type,
            self.current_quarter, self.time_remaining, time.time(),
            getattr(self, 'possession_team', ''), self.ball_holder
        ))
        self.db_conn.commit()

        if self.match_log_path:
            mins = int(self.time_remaining // 60)
            secs = int(self.time_remaining % 60)
            time_str = f"{mins}:{secs:02d}"
            q = f"Q{self.current_quarter}"
            team = getattr(self, 'possession_team', '')
            name = self.get_player_name(player_id)
            line = f"{time_str}\t{q}\t{team}\t\t{name}\t\t{event_type}\n"
            with open(self.match_log_path, "a", encoding="utf-8") as f:
                f.write(line)

    def update_stats_display(self):
        if not self.stats_text:
            return
        if not self.stats:
            self.stats_text.text = ""
            return

        event_types = set()
        for ev in self.stats.values():
            event_types.update(ev.keys())
        event_types = sorted(event_types)

        header = "Player".ljust(12) + " " + " ".join(et[:4].ljust(5) for et in event_types) + " Total"
        lines = [header, "-" * len(header)]

        rows = []
        for pid, ev in self.stats.items():
            total = sum(ev.values())
            name = self.get_player_name(pid)
            row = [name.ljust(12)]
            for et in event_types:
                row.append(str(ev.get(et, 0)).ljust(5))
            row.append(str(total))
            rows.append((total, " ".join(row)))

        for _, line in sorted(rows, key=lambda x: x[0], reverse=True)[:10]:
            lines.append(line)

        self.stats_text.text = "\n".join(lines)

    def generate_quarter_report(self):
        q = self.current_quarter
        evs = [e for e in self.critical_events if e['quarter'] == q]
        if not evs:
            self.log_message(f" Q{q} Report: No critical events")
            return

        counts = Counter(e['event'] for e in evs)
        home_c = sum(1 for e in evs if isinstance(e['player'], str) and e['player'].startswith('H-'))
        away_c = sum(1 for e in evs if isinstance(e['player'], str) and e['player'].startswith('A-'))

        report = f" Q{q} Report: {len(evs)} critical events (H:{home_c} A:{away_c})"
        top = counts.most_common(3)
        if top:
            report += " | " + ", ".join(f"{t[0]}:{t[1]}" for t in top)
        self.log_message(report)

        if self.match_log_path:
            with open(self.match_log_path, "a", encoding="utf-8") as f:
                f.write(f"\n--- Q{q} SUMMARY: {report} ---\n")
                for e in sorted(evs, key=lambda x: x['time'], reverse=True):
                    f.write(f"  {e['time_str']} {self.get_player_name(e['player'])} {e['event']}\n")

    # ------------ Popups: names, reports ------------

    def _simple_popup(self, title, message):
        content = BoxLayout(orientation='vertical')
        content.add_widget(Label(text=message))
        btn = Button(text="OK", size_hint_y=None, height='40dp')
        content.add_widget(btn)
        popup = Popup(title=title, content=content, size_hint=(0.8, 0.4), auto_dismiss=False)
        btn.bind(on_press=popup.dismiss)
        popup.open()

    def new_match_dialog(self):
        if self.names_required and not self.player_names_complete:
            self._simple_popup(
                " Names Required",
                "Please enter and save all 26 player names first ( Names)."
            )
            return

        content = BoxLayout(orientation='vertical', spacing=5, padding=5)
        content.add_widget(Label(text="New Match", size_hint_y=None, height='30dp'))
        content.add_widget(Label(text="Home Team:", size_hint_y=None, height='24dp'))
        home_entry = TextInput(text="Loughborough", multiline=False)
        content.add_widget(home_entry)
        content.add_widget(Label(text="Away Team:", size_hint_y=None, height='24dp'))
        away_entry = TextInput(text="", multiline=False)
        content.add_widget(away_entry)

        btn_row = BoxLayout(orientation='horizontal', size_hint_y=None, height='40dp')
        ok_btn = Button(text="OK")
        cancel_btn = Button(text="Cancel")
        btn_row.add_widget(ok_btn)
        btn_row.add_widget(cancel_btn)
        content.add_widget(btn_row)

        popup = Popup(title=" New Match", content=content,
                      size_hint=(0.8, 0.5), auto_dismiss=False)

        def on_ok(*_):
            home = home_entry.text.strip() or "Home"
            away = away_entry.text.strip() or "Away"
            self.start_new_match(home, away)
            popup.dismiss()

        ok_btn.bind(on_press=on_ok)
        cancel_btn.bind(on_press=lambda *_: popup.dismiss())
        popup.open()

    def start_new_match(self, home_team, away_team):
        now = datetime.now()
        match_code = now.strftime("%Y%m%d_%H%M%S")
        date_str = now.strftime("%Y-%m-%d %H:%M")
        final_score = ""
        self.db_conn.execute("""
            INSERT INTO matches (match_code, date, home_team, away_team, final_score)
            VALUES (?, ?, ?, ?, ?)
        """, (match_code, date_str, home_team, away_team, final_score))
        self.db_conn.commit()

        cur = self.db_conn.cursor()
        cur.execute("SELECT match_id FROM matches WHERE match_code=?", (match_code,))
        row = cur.fetchone()
        self.current_match_id = row[0] if row else None
        self.current_match_code = match_code

        self.match_log_path = os.path.join(self.data_dir, f"match_{match_code}.log")
        with open(self.match_log_path, "w", encoding="utf-8") as f:
            f.write(f"Match: {home_team} vs {away_team} ({date_str})\n")

        self.reset_quarter()
        self.reset_scores()
        self.log_message(f" New match started: {home_team} vs {away_team} (code {match_code})")

    def edit_names(self):
        """
        Kivy replacement for the tkinter 'Names' dialog:
        - 2 columns of 13 for Home (H-Player1..14) and Away (A-Player1..14)
        - Number + Name fields, saved to players table.
        """
        content = BoxLayout(orientation='vertical', spacing=5, padding=5)
        content.add_widget(Label(text="Edit Player Names", size_hint_y=None, height='30dp'))

        grids_row = BoxLayout(orientation='horizontal')

        # Home grid
        home_box = BoxLayout(orientation='vertical')
        home_box.add_widget(Label(text="Home", size_hint_y=None, height='24dp'))
        home_grid = GridLayout(cols=3, size_hint_y=None)
        home_grid.bind(minimum_height=home_grid.setter('height'))
        self._name_inputs_home = []
        for i in range(14):
            num = i + 1
            pid = f"H-Player{num}"
            home_grid.add_widget(Label(text=str(num), size_hint_y=None, height='28dp'))
            num_input = TextInput(text=str(num), multiline=False, size_hint_y=None, height='28dp')
            name_text = self.player_names.get(pid, "")
            name_input = TextInput(text=name_text, multiline=False, size_hint_y=None, height='28dp')
            self._name_inputs_home.append((pid, num_input, name_input))
            home_grid.add_widget(num_input)
            home_grid.add_widget(name_input)
        home_box.add_widget(home_grid)

        # Away grid
        away_box = BoxLayout(orientation='vertical')
        away_box.add_widget(Label(text="Away", size_hint_y=None, height='24dp'))
        away_grid = GridLayout(cols=3, size_hint_y=None)
        away_grid.bind(minimum_height=away_grid.setter('height'))
        self._name_inputs_away = []
        for i in range(14):
            num = i + 1
            pid = f"A-Player{num}"
            away_grid.add_widget(Label(text=str(num), size_hint_y=None, height='28dp'))
            num_input = TextInput(text=str(num), multiline=False, size_hint_y=None, height='28dp')
            name_text = self.player_names.get(pid, "")
            name_input = TextInput(text=name_text, multiline=False, size_hint_y=None, height='28dp')
            self._name_inputs_away.append((pid, num_input, name_input))
            away_grid.add_widget(num_input)
            away_grid.add_widget(name_input)
        away_box.add_widget(away_grid)

        grids_row.add_widget(home_box)
        grids_row.add_widget(away_box)
        content.add_widget(grids_row)

        btn_row = BoxLayout(orientation='horizontal', size_hint_y=None, height='40dp')
        save_btn = Button(text="Save All")
        cancel_btn = Button(text="Cancel")
        btn_row.add_widget(save_btn)
        btn_row.add_widget(cancel_btn)
        content.add_widget(btn_row)

        popup = Popup(title=" Player Names", content=content,
                      size_hint=(0.95, 0.9), auto_dismiss=False)

        def on_save(*_):
            cur = self.db_conn.cursor()
            # Clear existing players to avoid duplicates
            cur.execute("DELETE FROM players")

            # Insert home
            for pid, num_in, name_in in self._name_inputs_home:
                name = name_in.text.strip()
                num = int(num_in.text.strip() or "0")
                if name:
                    cur.execute(
                        "INSERT OR REPLACE INTO players (player_id, number, name, team) "
                        "VALUES (?, ?, ?, ?)",
                        (pid, num, name, "Home")
                    )

            # Insert away
            for pid, num_in, name_in in self._name_inputs_away:
                name = name_in.text.strip()
                num = int(num_in.text.strip() or "0")
                if name:
                    cur.execute(
                        "INSERT OR REPLACE INTO players (player_id, number, name, team) "
                        "VALUES (?, ?, ?, ?)",
                        (pid, num, name, "Away")
                    )

            self.db_conn.commit()
            self.player_names = self.load_player_names()

            # Check completeness
            home_ok = all(f"H-Player{i+1}" in self.player_names for i in range(14))
            away_ok = all(f"A-Player{i+1}" in self.player_names for i in range(14))
            self.player_names_complete = home_ok and away_ok
            popup.dismiss()

            msg = " All 26 names saved." if self.player_names_complete \
                  else "Names saved, but some players are still missing."
            self._simple_popup("Names Saved", msg)

        save_btn.bind(on_press=on_save)
        cancel_btn.bind(on_press=lambda *_: popup.dismiss())
        popup.open()

    def generate_report(self):
        """
        Simple match report popup:
        - For current match: total events, goals per team.
        - Top 10 players by goals.
        """
        if not self.current_match_id:
            self._simple_popup("Report", "Start a match first.")
            return

        cur = self.db_conn.cursor()
        cur.execute("SELECT home_team, away_team, final_score FROM matches WHERE match_id=?",
                    (self.current_match_id,))
        m = cur.fetchone()
        if m:
            home_team, away_team, final_score = m
        else:
            home_team = "Home"
            away_team = "Away"
            final_score = ""

        # Count events by type and team
        cur.execute("""
            SELECT player_id, event_type
            FROM events
            WHERE match_id=?
        """, (self.current_match_id,))
        rows = cur.fetchall()

        goals_home = goals_away = 0
        player_goals = defaultdict(int)
        event_counts = Counter()

        for pid, ev in rows:
            event_counts[ev] += 1
            if ev == 'Goal':
                player_goals[pid] += 1
                if isinstance(pid, str) and pid.startswith('H-'):
                    goals_home += 1
                elif isinstance(pid, str) and pid.startswith('A-'):
                    goals_away += 1

        lines = []
        lines.append(f"Match: {home_team} vs {away_team}")
        if final_score:
            lines.append(f"Final score: {final_score}")
        else:
            lines.append(f"Current score: {goals_home}-{goals_away}")

        lines.append("")
        lines.append("Event counts:")
        for ev, c in event_counts.most_common():
            lines.append(f"  {ev}: {c}")
        lines.append("")
        lines.append("Top scorers:")
        if player_goals:
            for pid, g in sorted(player_goals.items(), key=lambda x: x[1], reverse=True)[:10]:
                lines.append(f"  {self.get_player_name(pid)}: {g}")
        else:
            lines.append("  No goals yet.")

        content = BoxLayout(orientation='vertical')
        text = TextInput(text="\n".join(lines), readonly=True, multiline=True)
        content.add_widget(text)
        btn = Button(text="Close", size_hint_y=None, height='40dp')
        content.add_widget(btn)
        popup = Popup(title=" Match Report", content=content, size_hint=(0.9, 0.9))
        btn.bind(on_press=popup.dismiss)
        popup.open()

    def show_player_breakdown(self):
        """
        Player breakdown popup:
        - For current match: per-player summary of Goals, Shots, Foul, Excl.Win, etc.
        """
        if not self.current_match_id:
            self._simple_popup("Player Breakdown", "Start a match first.")
            return

        cur = self.db_conn.cursor()
        cur.execute("""
            SELECT player_id, event_type, COUNT(*)
            FROM events
            WHERE match_id=? AND player_id NOT LIKE 'GAME'
            GROUP BY player_id, event_type
        """, (self.current_match_id,))
        rows = cur.fetchall()

        per_player = defaultdict(lambda: defaultdict(int))
        for pid, ev, c in rows:
            per_player[pid][ev] = c

        metric_order = ['Goal', 'Shot', 'Pen.Win', 'Excl.Win', 'Foul', 'P.Lost', 'E.Lost', 'Block', 'Save']

        lines = []
        for pid, evs in sorted(per_player.items(), key=lambda x: self.get_player_name(x[0])):
            name = self.get_player_name(pid)
            team = "Home" if isinstance(pid, str) and pid.startswith('H-') else "Away"
            lines.append(f"{name} ({team})")
            totals = []
            for m in metric_order:
                if m in evs:
                    totals.append(f"{m}:{evs[m]}")
            if totals:
                lines.append("  " + ", ".join(totals))
            else:
                lines.append("  (no events)")
            lines.append("")

        if not lines:
            lines = ["No player events recorded yet."]

        content = BoxLayout(orientation='vertical')
        text = TextInput(text="\n".join(lines), readonly=True, multiline=True)
        content.add_widget(text)
        btn = Button(text="Close", size_hint_y=None, height='40dp')
        content.add_widget(btn)
        popup = Popup(title=" Player Breakdown", content=content, size_hint=(0.9, 0.9))
        btn.bind(on_press=popup.dismiss)
        popup.open()


class WaterPoloKivyApp(App):
    def build(self):
        root = WaterPoloRoot()
        controller = WaterPoloTrackerController(root)
        root.controller = controller
        return root


if __name__ == "__main__":
    WaterPoloKivyApp().run()
