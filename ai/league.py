# -*- coding: utf-8 -*-
"""引擎联赛核心：循环赛赛程、包干用时、积分榜、对局结果（不依赖 Qt）。

参考鲨鱼象棋 / 鹏飞象棋的"引擎联赛"：多个 UCI 引擎两两循环对弈，
可分「普通象棋联赛」与「揭棋联赛」，支持每方包干时间（超时判负）与积分排名。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from itertools import combinations

from core.constants import Mode, Side
from core.rules import GameResult


@dataclass
class LeaguePlayer:
    path: str
    name: str = ""
    note: str = ""
    score: float = 0.0
    wins: int = 0
    draws: int = 0
    losses: int = 0
    games: int = 0

    def __post_init__(self):
        if not self.name:
            self.name = os.path.basename(self.path)

    @property
    def record(self) -> str:
        return f"{self.wins}胜 {self.draws}和 {self.losses}负"

    def to_dict(self):
        return {"path": self.path, "name": self.name, "note": self.note,
                "score": self.score, "wins": self.wins, "draws": self.draws,
                "losses": self.losses, "games": self.games}

    @classmethod
    def from_dict(cls, d):
        return cls(d["path"], d.get("name", ""), d.get("note", ""),
                   float(d.get("score", 0)), int(d.get("wins", 0)),
                   int(d.get("draws", 0)), int(d.get("losses", 0)),
                   int(d.get("games", 0)))


@dataclass
class LeagueMatch:
    red: LeaguePlayer
    black: LeaguePlayer
    mode: Mode
    fen: str = ""
    seed: str = ""
    round_no: int = 1
    result: GameResult | None = None
    moves: list = field(default_factory=list)

    @property
    def label(self) -> str:
        return f"{self.red.name}（红） vs {self.black.name}（黑）"

    @property
    def status(self) -> str:
        return self.result.text if self.result else "未开始"

    def to_dict(self):
        return {"red": self.red.name, "black": self.black.name,
                "round": self.round_no,
                "result": self.result.to_dict() if self.result else None,
                "moves": [m.to_list() for m in self.moves]}


class League:
    def __init__(self, mode, players, rounds: int = 1, fen: str = "",
                 seed: str = "", movetime: int = 300, pack_seconds: float = 300,
                 score_win: float = 3.0, score_draw: float = 1.0,
                 score_loss: float = 0.0, depth: int = 10, hash_mb: int = 64,
                 threads: int = 1, chase_rule: str = "skyrule"):
        self.mode = Mode(mode)
        self.players = list(players)
        if len(self.players) < 2:
            raise ValueError("联赛至少需要 2 个参赛引擎")
        self.rounds = max(1, int(rounds))
        self.fen = fen or ""
        self.seed = seed or ""
        self.movetime = int(movetime)
        self.pack_seconds = float(pack_seconds)   # 每方包干时间（秒），0 = 不限
        self.score_win = float(score_win)
        self.score_draw = float(score_draw)
        self.score_loss = float(score_loss)
        self.depth = int(depth)
        self.hash_mb = int(hash_mb)
        self.threads = int(threads)
        self.chase_rule = chase_rule
        self.schedule: list[LeagueMatch] = []
        self.current_index = -1
        self.finished = False
        self._build()

    def _build(self):
        n = len(self.players)
        for r in range(1, self.rounds + 1):
            for i, j in combinations(range(n), 2):
                # 偶数轮交换先后手，保证公平
                if r % 2 == 1:
                    red, black = self.players[i], self.players[j]
                else:
                    red, black = self.players[j], self.players[i]
                self.schedule.append(LeagueMatch(
                    red=red, black=black, mode=self.mode,
                    fen=self.fen, seed=self.seed, round_no=r))

    def next_match(self) -> LeagueMatch | None:
        self.current_index += 1
        if self.current_index >= len(self.schedule):
            self.finished = True
            return None
        return self.schedule[self.current_index]

    def current_match(self) -> LeagueMatch | None:
        if 0 <= self.current_index < len(self.schedule):
            return self.schedule[self.current_index]
        return None

    def apply_result(self, match: LeagueMatch, result: GameResult):
        match.result = result
        red, black = match.red, match.black
        red.games += 1
        black.games += 1
        if result.winner is None:
            red.draws += 1
            black.draws += 1
            red.score += self.score_draw
            black.score += self.score_draw
        elif result.winner == Side.RED:
            red.wins += 1
            black.losses += 1
            red.score += self.score_win
            black.score += self.score_loss
        else:
            black.wins += 1
            red.losses += 1
            black.score += self.score_win
            red.score += self.score_loss

    def standings(self) -> list[LeaguePlayer]:
        return sorted(self.players,
                      key=lambda p: (-p.score, -p.wins, p.losses, p.name))

    def summary(self) -> str:
        lines = [f"联赛模式：{self.mode.label} · {self.rounds} 轮循环 · "
                 f"共 {len(self.schedule)} 局",
                 f"包干用时：{self.pack_seconds / 60:.1f} 分/方 · "
                 f"每步 {self.movetime} ms", "",
                 f"{'排名':<5}{'引擎':<28}{'积分':>7}{'胜':>5}{'和':>5}{'负':>5}"]
        lines.append("-" * 56)
        for i, p in enumerate(self.standings(), 1):
            lines.append(f"{i:<5}{p.name[:26]:<28}{p.score:>7.1f}"
                         f"{p.wins:>5}{p.draws:>5}{p.losses:>5}")
        lines.append("")
        lines.append("对局结果：")
        for k, m in enumerate(self.schedule, 1):
            lines.append(f"  {k:2d}. [第{m.round_no}轮] {m.label} — {m.status}")
        return "\n".join(lines)

    def to_dict(self):
        return {
            "app": "飞鲨象棋联赛", "version": 1,
            "mode": int(self.mode), "rounds": self.rounds,
            "fen": self.fen, "seed": self.seed,
            "movetime": self.movetime, "pack_seconds": self.pack_seconds,
            "score": [self.score_win, self.score_draw, self.score_loss],
            "players": [p.to_dict() for p in self.players],
            "schedule": [m.to_dict() for m in self.schedule],
            "summary": self.summary(),
        }
