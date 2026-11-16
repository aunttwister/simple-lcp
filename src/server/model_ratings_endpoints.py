# ── Model ratings / leaderboard API (operator's model-leaderboard design) ────
#
# The leaderboard is POSITION, not accuracy: Elo (text boards) or net improvement
# (agent boards) plus a percentile, in its own store (`model_ratings`). Nothing in
# this class writes `model_capabilities` — grading and routing read that table and
# are deliberately untouched by a leaderboard refresh.
#
# The refresh is staged, never applied: POST /poll fetches the boards and stages
# an old -> new diff (both dates), POST /update accepts the ticked rows, POST
# /clear discards them. Same shape as the model-intel poll's `status='new'`
# deployments — a machine reading is staged for review, never written over the
# human-owned value.

from typing import Any


class ModelRatingsEndpoints:
    """Leaderboard tabs + the poll -> diff -> accept column.

    Tabs are namespaced by construction: arena rows carry an axis name, every
    operator-defined taxonomy is `custom:<slug>`, so a poll can never overwrite a
    custom board and an import can never pollute an arena one.
    """

    config: Any
    engine: Any

    # ── helpers ──
    def _mr_connect(self):
        from ..api.model_identity import connect as mi_connect
        return mi_connect()

    def _mr_visibles(self, con):
        from ..api.model_ratings import visible_models
        return sorted(visible_models(con))

    def _mr_leaderboard_payload(self, con, taxonomy: str) -> dict:
        from ..api.model_ratings import leaderboard, staged_rows, taxonomies
        rows = leaderboard(con, taxonomy) if taxonomy else []
        return {
            "taxonomy": taxonomy,
            "tabs": taxonomies(con),
            "rows": rows,
            "units": sorted({r["unit"] for r in rows}),
            "eligible": self._mr_visibles(con),
            "staged": len(staged_rows(con)),
        }

    # ── reads ──
    def _serve_model_ratings_leaderboard_api(self):
        """GET /api/model-ratings/leaderboard[?taxonomy=axis|custom:name] — column 2."""
        from urllib.parse import parse_qs, urlparse
        from ..api.model_ratings import taxonomies

        con = self._mr_connect()
        try:
            qs = parse_qs(urlparse(self.path).query)
            want = (qs.get("taxonomy") or [""])[0].strip()
            tabs = taxonomies(con)
            if not want:
                want = tabs[0]["taxonomy"] if tabs else ""
            self._send_json(self._mr_leaderboard_payload(con, want))
        finally:
            con.close()

    def _serve_model_ratings_taxonomies_api(self):
        """GET /api/model-ratings/taxonomies — the tab strip."""
        from ..api.model_ratings import taxonomies

        con = self._mr_connect()
        try:
            self._send_json({"tabs": taxonomies(con)})
        finally:
            con.close()

    def _serve_model_ratings_staged_api(self):
        """GET /api/model-ratings/staged[?taxonomy=] — the pending diff, if any."""
        from urllib.parse import parse_qs, urlparse
        from ..api.model_ratings import staged_rows, taxonomies

        con = self._mr_connect()
        try:
            qs = parse_qs(urlparse(self.path).query)
            want = (qs.get("taxonomy") or [""])[0].strip() or None
            rows = staged_rows(con, want)
            self._send_json({"rows": rows, "count": len(rows),
                             "taxonomies": taxonomies(con)})
        finally:
            con.close()

    # ── the poll column ──
    def _serve_model_ratings_poll_api(self):
        """POST /api/model-ratings/poll — fetch the boards and stage the diff.

        Writes only ``rating_poll_staging``. The published baseline is untouched
        until /update accepts rows.
        """
        from ..api.model_ratings import stage_poll

        con = self._mr_connect()
        try:
            try:
                result = stage_poll(con)
            except Exception as exc:  # noqa: BLE001 — a failed fetch is reportable, not fatal
                self._send_json(
                    {"error": f"{type(exc).__name__}: {exc}",
                     "hint": "the boards are read from the dataset's parquet files; "
                             "a failure here is upstream and re-runnable"},
                    502)
                return
        finally:
            con.close()
        self._audit("model_ratings.poll", staged=result.get("staged"))
        self._send_json({"ok": True, **result})

    def _serve_model_ratings_update_api(self):
        """POST /api/model-ratings/update — accept the ticked rows as the baseline."""
        from ..api.model_ratings import commit_staged

        body = self._read_body()
        keys = body.get("keys")
        if keys is not None and not isinstance(keys, list):
            self._send_json({"error": "keys must be a list of row identities (or omit "
                                      "the field to accept every staged row)"}, 400)
            return
        con = self._mr_connect()
        try:
            result = commit_staged(con, keys)
        except ValueError as exc:
            self._send_json({"error": str(exc)}, 400)
            return
        finally:
            con.close()
        self._audit("model_ratings.update", accepted=result.get("written"))
        self._send_json({"ok": True, **result})

    def _serve_model_ratings_clear_api(self):
        """POST /api/model-ratings/clear — discard the polled results."""
        from ..api.model_ratings import clear_staged, taxonomies

        con = self._mr_connect()
        try:
            result = clear_staged(con)
            result["taxonomies"] = taxonomies(con)
        finally:
            con.close()
        self._audit("model_ratings.clear", cleared=result.get("cleared"))
        self._send_json({"ok": True, **result})

    # ── the mapping: which LMArena board feeds which taxonomy ──
    def _serve_model_ratings_catalogue_api(self):
        """GET /api/model-ratings/catalogue — every board LMArena publishes.

        Split into `mapped` (feeding at least one of the operator's taxonomies) and
        `unmapped` (visible but unused, with row counts), because "this board scores
        11 of yours and nothing maps it" is the information that makes the mapping
        decidable.
        """
        from ..api.arena_taxonomy import catalogue, mapping

        con = self._mr_connect()
        try:
            self._send_json({**catalogue(con), "mapping": mapping(con)})
        finally:
            con.close()

    def _serve_model_ratings_map_api(self):
        """GET/POST/DELETE /api/model-ratings/map — the operator's mapping.

        POST {taxonomy, board, note?} maps one board onto one taxonomy AND publishes
        it immediately from the stored catalogue (no re-poll — that is the point of
        persisting every board first).
        DELETE ?taxonomy=&board= unmaps and removes the rows it produced.
        """
        from urllib.parse import parse_qs, urlparse
        from ..api.arena_taxonomy import catalogue, map_board, mapping, unmap_board

        if self.command == "GET":
            con = self._mr_connect()
            try:
                self._send_json({"mapping": mapping(con), **catalogue(con)})
            finally:
                con.close()
            return

        con = self._mr_connect()
        try:
            if self.command == "DELETE":
                qs = parse_qs(urlparse(self.path).query)
                tax = (qs.get("taxonomy") or [""])[0].strip()
                board = (qs.get("board") or [""])[0].strip()
                if not tax or not board:
                    self._send_json({"error": "taxonomy and board are required"}, 400)
                    return
                out = unmap_board(con, tax, board)
                code = 200
            else:
                body = self._read_body()
                tax = (body.get("taxonomy") or "").strip()
                board = (body.get("board") or "").strip()
                out = map_board(con, tax, board, note=body.get("note"))
                code = 200
        except ValueError as exc:
            self._send_json({"error": str(exc)}, 400)
            return
        finally:
            con.close()
        self._audit(f"model_ratings.map.{self.command.lower()}",
                    taxonomy=locals().get("tax"), board=locals().get("board"))
        self._send_json({"ok": True, **out, "tabs": self._mr_tabs()}, code)

    def _mr_tabs(self):
        from ..api.model_ratings import taxonomies
        con = self._mr_connect()
        try:
            return taxonomies(con)
        finally:
            con.close()

    # ── manage taxonomies (column 3) ──
    def _serve_model_ratings_taxonomy_api(self):
        """GET /api/model-ratings/taxonomy — the tabs, with their kind and row counts.

        POST {action:'define'|'import'|'rate', …} — define a custom board, import a
        CSV of ratings, or set ratings by hand. Every taxonomy written here is
        coerced into the `custom:` namespace.
        """
        from ..api.model_ratings import (define_taxonomy, import_csv,
                                         leaderboard, set_custom_ratings, taxonomies)

        if self.command != "POST":
            con = self._mr_connect()
            try:
                self._send_json({"tabs": taxonomies(con),
                                 "eligible": self._mr_visibles(con)})
            finally:
                con.close()
            return

        body = self._read_body()
        action = (body.get("action") or "").strip()
        con = self._mr_connect()
        try:
            if action == "define":
                name = (body.get("name") or "").strip()
                if not name:
                    self._send_json({"error": "name required"}, 400)
                    return
                out = define_taxonomy(con, name, label=body.get("label"),
                                      note=body.get("note"))
            elif action == "import":
                text = body.get("csv")
                if not isinstance(text, str) or not text.strip():
                    self._send_json({"error": "csv (text) required"}, 400)
                    return
                out = import_csv(con, text, taxonomy=body.get("taxonomy"))
            elif action == "rate":
                rows = body.get("rows")
                if not isinstance(rows, list) or not rows:
                    self._send_json({"error": "rows required"}, 400)
                    return
                out = set_custom_ratings(con, body.get("taxonomy") or "", rows)
                out["leaderboard"] = leaderboard(con, out["taxonomy"])
            else:
                self._send_json({"error": "action must be define, import or rate"}, 400)
                return
            # read back what is in force — the tab strip re-renders from this
            out["tabs"] = taxonomies(con)
        except ValueError as exc:
            self._send_json({"error": str(exc)}, 400)
            return
        finally:
            con.close()
        self._audit(f"model_ratings.taxonomy.{action}")
        self._send_json({"ok": True, **out})
