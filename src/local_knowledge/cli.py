import argparse
import json
import logging
import os
from pathlib import Path
import sys
from .config import Config, config_path
from .database import connect, doctor, check_identity
from .embeddings import GemmaProvider, prefetch
from .ingestion import import_file, selected_files, remove, rebuild, writer_lock
from .retrieval import search, open_document, list_documents


def parser():
    p = argparse.ArgumentParser(prog="knowledge", description="Local evidence retrieval; indexed text is untrusted.")
    p.add_argument("--config", type=Path, default=config_path(), help="TOML config (XDG default)")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("init", help="Create configuration and schema")
    sub.add_parser("prefetch", help="Explicit network download; pin immutable model revision")
    add = sub.add_parser("add", help="Import selected file or directory recursively")
    add.add_argument("path", type=Path)
    add.add_argument("--tag", action="append", default=[])
    add.add_argument("--source-url")
    add.add_argument("--title", help="Explicit document title; use with a selected file")
    add.add_argument("--publication-date", help="Reliable known publication date (YYYY-MM-DD)")
    add.add_argument("--project", help="Explicit project/product described by this source")
    add.add_argument("--source-version", help="Explicit upstream source version; not an inferred installed version")
    add.add_argument("--collection", help="Explicit collection identity (independent of search tags)")
    find = sub.add_parser("search", help="Hybrid keyword + semantic search")
    find.add_argument("query")
    find.add_argument("--limit", type=int)
    find.add_argument("--tag", action="append", default=[])
    find.add_argument("--document-id")
    find.add_argument("--source-type")
    find.add_argument("--published-after")
    find.add_argument("--published-before")
    view = sub.add_parser("open", help="Resolve document/chunk citations")
    view.add_argument("document_id", nargs="?")
    view.add_argument("--chunk-id")
    view.add_argument("--offset", type=int, default=0)
    view.add_argument("--limit", type=int, default=3)
    listing = sub.add_parser("list", help="List source metadata")
    listing.add_argument("--tag")
    listing.add_argument("--limit", type=int, default=50)
    listing.add_argument("--offset", type=int, default=0)
    delete = sub.add_parser("remove", help="Delete indexed records; original file stays intact")
    delete.add_argument("document_id")
    delete.add_argument("--force", action="store_true")
    sub.add_parser("stats", help="Counts, storage and embedding identity")
    sub.add_parser("doctor", help="Check DB, FTS, vectors, model and runtime")
    sub.add_parser("rebuild", help="Atomically rebuild from retained extraction snapshots")
    sub.add_parser("serve", help="Run read-only MCP over stdio")
    return p


def run(args):
    config = Config.load(args.config)
    logging.basicConfig(level=config.logging_level, stream=sys.stderr)
    provider = GemmaProvider(config)
    if args.command == "init":
        with writer_lock(config):
            connect(config).close()
            os.chmod(config.database_path, 0o600)
            if not args.config.exists():
                config.save(args.config)
        return {"config": str(args.config), "database": config.database_path}
    if args.command == "prefetch":
        revision = prefetch(config)
        config.save(args.config)
        # Verify the actual loader and embeddings, not just downloaded file names.
        query = provider.embed_query("local knowledge retrieval")
        provider.embed_documents([("Verification", "A local library stores selected reference documents.")])
        return {"revision": revision, "dimensions": len(query), "device": "cpu", "precision": "float32"}
    if args.command == "add":
        results = []
        failed = False
        for path in selected_files(args.path):
            try:
                metadata = {key: value for key, value in {"project": args.project, "version": args.source_version, "collection": args.collection}.items() if value}
                results.append({"path": str(path), **import_file(config, provider, path, args.tag, args.source_url, args.publication_date, args.title, source_metadata=metadata or None)})
            except Exception as error:
                failed = True
                results.append({"path": str(path), "status": "failed", "error": str(error)[:500]})
        print(json.dumps(results, indent=2))
        return 1 if failed else 0
    if args.command == "search":
        return search(config, provider, args.query, args.limit, tags=args.tag, document_id=args.document_id,
                      source_type=args.source_type, published_after=args.published_after, published_before=args.published_before)
    if args.command == "open":
        return open_document(config, args.document_id, args.chunk_id, args.offset, args.limit)
    if args.command == "list":
        return list_documents(config, args.tag, args.limit, args.offset)
    if args.command == "remove":
        if not args.force and input(f"Remove indexed document {args.document_id}? [y/N] ").lower() != "y":
            return {"status": "cancelled"}
        remove(config, args.document_id)
        return {"status": "removed"}
    if args.command == "rebuild":
        rebuild(config, provider)
        return {"status": "rebuilt"}
    if args.command == "serve":
        from .mcp_server import create_server
        create_server(config, provider).run(transport="stdio")
        return 0
    db = connect(config, readonly=True)
    try:
        if args.command == "stats":
            return {"documents": db.execute("SELECT count(*) FROM documents").fetchone()[0],
                    "chunks": db.execute("SELECT count(*) FROM chunks").fetchone()[0],
                    "database_bytes": Path(config.database_path).stat().st_size,
                    "index_metadata": {r[0]: json.loads(r[1]) if r[0] == "index_identity" else r[1] for r in db.execute("SELECT * FROM metadata WHERE key NOT LIKE 'source:%'")},
                    "model_id": config.model_id, "revision": config.model_revision, "device": "cpu", "precision": "float32"}
        report = doctor(db, config.dimensions)
        try:
            provider.count_tokens("offline availability check")
            # Lazy weights remain unloaded in normal doctor; real test is optional.
            from importlib.metadata import version
            report["sentence_transformers"] = version("sentence-transformers")
            report["transformers"] = version("transformers")
            if db.execute("SELECT 1 FROM chunks LIMIT 1").fetchone():
                check_identity(db, config, provider)
            report["model"] = "local snapshot/tokenizer available; weight execution requires real-model test"
        except Exception as error:
            report["ok"] = False
            report["issues"].append(str(error))
        print(json.dumps(report, indent=2))
        return 0 if report["ok"] else 1
    finally:
        db.close()


def main():
    # Private generated files by default. Does not alter caller shell state.
    os.umask(0o077)
    try:
        result = run(parser().parse_args())
        if isinstance(result, int):
            return result
        print(json.dumps(result, indent=2, ensure_ascii=True))
        return 0
    except (Exception, KeyboardInterrupt) as error:
        print(f"knowledge: {type(error).__name__}: {str(error)[:500]}", file=sys.stderr)
        return 130 if isinstance(error, KeyboardInterrupt) else 1


if __name__ == "__main__":
    sys.exit(main())
