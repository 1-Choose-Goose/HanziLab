"""Общая схема и построение индексов для всех генераторов словаря."""

from __future__ import annotations

import sqlite3

try:
    from scripts.text_normalization import (
        normalize_translation,
        normalized_pinyin_variants,
    )
except ModuleNotFoundError:
    from text_normalization import normalize_translation, normalized_pinyin_variants


def create_dictionary_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE entries (
            hanzi TEXT NOT NULL UNIQUE,
            pinyin TEXT NOT NULL,
            translation TEXT NOT NULL
        );
        CREATE TABLE examples (
            chinese TEXT NOT NULL,
            pinyin TEXT NOT NULL,
            translation TEXT NOT NULL DEFAULT '',
            UNIQUE(chinese, translation)
        );
        CREATE VIRTUAL TABLE entries_fts USING fts5(
            hanzi, pinyin, translation, content='', tokenize='trigram'
        );
        CREATE VIRTUAL TABLE examples_fts USING fts5(
            chinese, pinyin, translation, content='', tokenize='trigram'
        );
        """
    )


def build_indexes(connection: sqlite3.Connection, *, tables: tuple[str, ...] = ("entries", "examples")) -> None:
    for table in tables:
        if table not in {"entries", "examples"}:
            raise ValueError(f"Unknown dictionary table: {table}")
        columns = ("hanzi" if table == "entries" else "chinese", "pinyin", "translation")
        fts = f"{table}_fts"
        # Rebuilding an existing contentless index must discard its old tokens.
        connection.execute(f"INSERT INTO {fts}({fts}) VALUES ('delete-all')")
        cursor = connection.execute(
            f"SELECT rowid, {', '.join(columns)} FROM {table} ORDER BY rowid"
        )
        try:
            while rows := cursor.fetchmany(5000):
                connection.executemany(
                    f"INSERT INTO {fts}(rowid, {', '.join(columns)}) VALUES (?, ?, ?, ?)",
                    (
                        (rowid, first, normalized_pinyin_variants(pinyin), normalize_translation(translation))
                        for rowid, first, pinyin, translation in rows
                    ),
                )
        finally:
            cursor.close()
