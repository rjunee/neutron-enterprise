// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0

import { useCallback, useState } from "react";
import { useSearchParams } from "react-router";
import { usePrincipal } from "../../auth/PrincipalContext";
import { useApiClient } from "../../auth/useApiClient";
import { ListScreen } from "../../primitives/PageHeader";
import { LoadError, Loading } from "../../primitives/states";
import { DocumentsWorkspace } from "./DocumentsWorkspace";
import type { DocumentDirectorySummary } from "./DocumentsWorkspace";
import { parseDocumentRouteState, serializeDocumentRouteState } from "./documentRouteState";
import type { DocumentRouteState } from "./documentRouteState";
import "./documents.css";

export function DocumentsPage() {
  const client = useApiClient();
  const principal = usePrincipal();
  const [searchParams, setSearchParams] = useSearchParams();
  const route = parseDocumentRouteState(searchParams);
  /* THE FACTS MARGIN beside the title. It is `null` until the directory read
   * settles, and the title row then renders NOTHING rather than a zero: a count
   * shown while the list is loading, refused or failed would be a number about
   * a window nobody has read. */
  const [directory, setDirectory] = useState<DocumentDirectorySummary | null>(null);
  const onDirectorySummary = useCallback((next: DocumentDirectorySummary | null) => {
    setDirectory((previous) =>
      previous !== null &&
      next !== null &&
      previous.loaded === next.loaded &&
      previous.more === next.more
        ? previous
        : next,
    );
  }, []);
  const directoryMeta =
    directory === null
      ? undefined
      : directory.more
        ? `${directory.loaded} documents loaded · more available`
        : `${directory.loaded} document${directory.loaded === 1 ? "" : "s"} visible`;

  const changeRoute = useCallback(
    (next: DocumentRouteState, options?: { replace?: boolean }) => {
      setSearchParams(serializeDocumentRouteState(next, searchParams), {
        replace: options?.replace ?? false,
      });
    },
    [searchParams, setSearchParams],
  );

  if (principal.status === "loading") {
    return (
      <ListScreen title="Documents">
        <Loading things="the signed-in identity" />
      </ListScreen>
    );
  }
  if (principal.status === "error" || principal.principal === null) {
    return (
      <ListScreen title="Documents">
        <LoadError things="the signed-in identity" onRetry={() => window.location.reload()} />
      </ListScreen>
    );
  }
  if (route.selector.kind === "invalid") {
    return (
      <ListScreen title="Documents">
        <div className="ne-state ne-state--error" role="alert">
          <p className="ne-state__line">The document version selector is invalid.</p>
          <button
            type="button"
            className="ne-btn ne-btn--quiet"
            onClick={() =>
              changeRoute({ ...route, selector: { kind: "latest" } }, { replace: true })
            }
          >
            Show latest visible version
          </button>
        </div>
      </ListScreen>
    );
  }

  return (
    <ListScreen title="Documents" meta={directoryMeta}>
      <DocumentsWorkspace
        client={client}
        ownerId={principal.principal.id}
        route={route}
        onRouteChange={changeRoute}
        onDirectorySummary={onDirectorySummary}
      />
    </ListScreen>
  );
}
