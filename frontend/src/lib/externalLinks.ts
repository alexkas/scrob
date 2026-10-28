// User-defined badges on movie/show pages (Settings → External links).
// Mirrors schemas.ExternalLink on the backend.

export interface ExternalLink {
  label: string;
  url_template: string;
}

export interface ExternalLinkIds {
  imdb_id?: string | null;
  tmdb_id?: number | null;
  tvdb_id?: number | null;
  // Stremio's naming: a show is "series".
  type: "movie" | "series";
}

export interface ResolvedExternalLink {
  label: string;
  href: string;
  // App links (stremio://) hand off to the app; a new tab would stay blank.
  newTab: boolean;
}

export const EXTERNAL_LINK_PLACEHOLDERS = ["imdb_id", "tmdb_id", "tvdb_id", "type"] as const;

// Same block list as the backend - re-checked here so a bad row in the
// database can never become a script link.
const BLOCKED_SCHEMES = new Set(["javascript", "data", "vbscript", "file", "blob"]);

function linkScheme(url: string): string | null {
  return /^([a-zA-Z][a-zA-Z0-9+.-]*):/.exec(url)?.[1].toLowerCase() ?? null;
}

/** Fill each template's placeholders. A link is skipped when one of its
 *  placeholders has no value for this title (e.g. no IMDb id). */
export function resolveExternalLinks(
  links: ExternalLink[] | null | undefined,
  ids: ExternalLinkIds,
): ResolvedExternalLink[] {
  const values: Record<string, string | null> = {
    imdb_id: ids.imdb_id || null,
    tmdb_id: ids.tmdb_id != null ? String(ids.tmdb_id) : null,
    tvdb_id: ids.tvdb_id != null ? String(ids.tvdb_id) : null,
    type: ids.type,
  };
  const resolved: ResolvedExternalLink[] = [];
  for (const link of links ?? []) {
    if (!link?.label || !link?.url_template) continue;
    let missing = false;
    const href = link.url_template.replace(/\{([^{}]*)\}/g, (_, name: string) => {
      const value = values[name];
      if (value == null) {
        missing = true;
        return "";
      }
      return encodeURIComponent(value);
    });
    const scheme = linkScheme(href);
    if (missing || !scheme || BLOCKED_SCHEMES.has(scheme)) continue;
    resolved.push({ label: link.label, href, newTab: scheme === "http" || scheme === "https" });
  }
  return resolved;
}
