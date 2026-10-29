#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Created on 2025-06-04 23:01:38 Wednesday

@author: Nikhil Kapila
"""

from typing import List, Dict
import ipaddress
from urllib.parse import urlparse

def sort_by_score(results: List[Dict]) -> List[Dict]:
    """Sort results by similarity score."""
    return sorted(results, key=lambda x: x['score'], reverse=True)
# --- domain filters ------------------------------------------------------------

def clean_domains(domains: List[str] | None) -> List[str]:
    """"https://www.Example.com/Docs/" -> "example.com/docs", so the model can pass URLs,
    hosts, or a host plus a path to narrow it to one part of a site."""
    out = []
    for d in domains or []:
        d = d.strip()
        parsed = urlparse(d if "//" in d else f"//{d}")
        host = (parsed.hostname or "").lower().removeprefix("www.")
        path = parsed.path.strip("/").lower()
        entry = f"{host}/{path}" if host and path else host
        if entry and entry not in out:
            out.append(entry)
    return out

def _host_in(url: str, domains: List[str]) -> bool:
    parsed = urlparse(url)
    host, path = (parsed.hostname or "").lower(), parsed.path.lower().rstrip("/")
    for d in domains:
        d_host, _, d_path = d.partition("/")
        # subdomains count: docs.python.org is under python.org
        if host != d_host and not host.endswith(f".{d_host}"):
            continue
        # a path narrows it to that part of the site: /docs, /docs/x, not /docsearch
        if not d_path or path == f"/{d_path}" or path.startswith(f"/{d_path}/"):
            return True
    return False

def preferred(url: str, prefer: List[str]) -> bool:
    return bool(prefer) and _host_in(url, prefer)

def domain_ok(url: str, include: List[str], exclude: List[str]) -> bool:
    return (not include or _host_in(url, include)) and not _host_in(url, exclude)

def site_query(query: str, include: List[str]) -> str:
    """Ask the engines for the domains up front. Not every engine honours site:,
    so results still go through domain_ok afterwards."""
    if not include:
        return query
    sites = " OR ".join(f"site:{d}" for d in include)
    return f"{query} {sites}" if len(include) == 1 else f"{query} ({sites})"

def public_url(url: str) -> bool:
    """http(s) and not obviously local. read_url takes URLs straight from the model, and a
    page could talk it into reading a router admin page or a dev server otherwise. Names
    that resolve to private IPs still get through: this catches the obvious cases only."""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in ("http", "https") or not host:
        return False
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return True
    return ip.is_global
