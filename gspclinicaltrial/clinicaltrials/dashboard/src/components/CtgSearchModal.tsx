import { useState, useCallback, useRef } from "react";
import {
  Search,
  Loader2,
  Download,
  ExternalLink,
  ChevronDown,
  ChevronUp,
  FlaskConical,
  Users,
  Calendar,
  Building2,
  Filter,
} from "lucide-react";
import Modal from "./Modal";
import Badge from "./Badge";
import type { CtgStudy, CtgStudyDetail, CtgImportResult } from "../api";
import * as api from "../api";
import styles from "./CtgSearchModal.module.css";

interface CtgSearchModalProps {
  open: boolean;
  onClose: () => void;
  onImported: (result: CtgImportResult) => void;
}

const STATUS_OPTIONS = [
  { value: "", label: "Any status" },
  { value: "RECRUITING", label: "Recruiting" },
  { value: "ACTIVE_NOT_RECRUITING", label: "Active, not recruiting" },
  { value: "COMPLETED", label: "Completed" },
  { value: "NOT_YET_RECRUITING", label: "Not yet recruiting" },
  { value: "ENROLLING_BY_INVITATION", label: "Enrolling by invitation" },
];

const PHASE_OPTIONS = [
  { value: "", label: "Any phase" },
  { value: "PHASE1", label: "Phase 1" },
  { value: "PHASE2", label: "Phase 2" },
  { value: "PHASE3", label: "Phase 3" },
  { value: "PHASE4", label: "Phase 4" },
  { value: "EARLY_PHASE1", label: "Early Phase 1" },
];

function statusBadgeVariant(status: string): "eligible" | "borderline" | "info" | "neutral" {
  if (status === "RECRUITING" || status === "ENROLLING_BY_INVITATION") return "eligible";
  if (status === "ACTIVE_NOT_RECRUITING" || status === "NOT_YET_RECRUITING") return "borderline";
  if (status === "COMPLETED") return "info";
  return "neutral";
}

export default function CtgSearchModal({ open, onClose, onImported }: CtgSearchModalProps) {
  const [query, setQuery] = useState("");
  const [condition, setCondition] = useState("");
  const [statusFilter, setStatusFilter] = useState("");
  const [phaseFilter, setPhaseFilter] = useState("");
  const [showFilters, setShowFilters] = useState(false);

  const [results, setResults] = useState<CtgStudy[]>([]);
  const [totalCount, setTotalCount] = useState(0);
  const [nextPageToken, setNextPageToken] = useState("");
  const [searching, setSearching] = useState(false);
  const [searched, setSearched] = useState(false);

  const [expandedNct, setExpandedNct] = useState<string | null>(null);
  const [studyDetail, setStudyDetail] = useState<CtgStudyDetail | null>(null);
  const [loadingDetail, setLoadingDetail] = useState(false);

  const [importingNct, setImportingNct] = useState<string | null>(null);
  const [importError, setImportError] = useState<string | null>(null);

  const detailControllerRef = useRef<AbortController | null>(null);

  const handleSearch = useCallback(async (pageToken = "") => {
    setSearching(true);
    setSearched(true);
    try {
      const data = await api.searchCtg({
        query,
        condition,
        status: statusFilter,
        phase: phaseFilter,
        pageSize: 10,
        pageToken,
      });
      if (pageToken) {
        setResults((prev) => [...prev, ...data.studies]);
      } else {
        setResults(data.studies);
      }
      setTotalCount(data.totalCount);
      setNextPageToken(data.nextPageToken);
    } catch {
      if (!pageToken) {
        setResults([]);
        setTotalCount(0);
      }
    } finally {
      setSearching(false);
    }
  }, [query, condition, statusFilter, phaseFilter]);

  const handleExpand = async (nctId: string) => {
    detailControllerRef.current?.abort();
    if (expandedNct === nctId) {
      setExpandedNct(null);
      setStudyDetail(null);
      return;
    }
    setExpandedNct(nctId);
    setLoadingDetail(true);
    const controller = new AbortController();
    detailControllerRef.current = controller;
    try {
      const detail = await api.fetchCtgStudy(nctId, controller.signal);
      if (!controller.signal.aborted) setStudyDetail(detail);
    } catch {
      if (!controller.signal.aborted) setStudyDetail(null);
    } finally {
      if (!controller.signal.aborted) setLoadingDetail(false);
    }
  };

  const handleImport = async (nctId: string) => {
    setImportingNct(nctId);
    setImportError(null);
    try {
      const result = await api.importCtgTrial(nctId);
      if (result.status === "imported") {
        onImported(result);
        onClose();
      }
    } catch {
      setImportError(`Failed to import ${nctId}. Please try again.`);
    } finally {
      setImportingNct(null);
    }
  };

  return (
    <Modal open={open} onClose={onClose} title="Search ClinicalTrials.gov" width={900}>
      <div className={styles.body}>
        {/* Search bar */}
        <div className={styles.searchRow}>
          <div className={styles.searchInputWrap}>
            <Search size={16} className={styles.searchIcon} />
            <input
              type="text"
              className={styles.searchInput}
              placeholder="Search by keyword, NCT number, or drug name…"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && handleSearch()}
              aria-label="Search ClinicalTrials.gov"
            />
          </div>
          <div className={styles.searchInputWrap} style={{ maxWidth: 240 }}>
            <FlaskConical size={14} className={styles.searchIcon} />
            <input
              type="text"
              className={styles.searchInput}
              placeholder="Condition (e.g. diabetes)"
              value={condition}
              onChange={(e) => setCondition(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && handleSearch()}
              aria-label="Filter by condition"
            />
          </div>
          <button className={styles.searchBtn} onClick={() => handleSearch()} disabled={searching}>
            {searching ? <Loader2 size={16} className={styles.spin} /> : <Search size={16} />}
            Search
          </button>
          <button
            className={styles.filterToggle}
            onClick={() => setShowFilters((f) => !f)}
            aria-label="Toggle filters"
          >
            <Filter size={14} />
          </button>
        </div>

        {/* Filters */}
        {showFilters && (
          <div className={styles.filterRow}>
            <select className={styles.filterSelect} value={statusFilter} onChange={(e) => setStatusFilter(e.target.value)}>
              {STATUS_OPTIONS.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
            </select>
            <select className={styles.filterSelect} value={phaseFilter} onChange={(e) => setPhaseFilter(e.target.value)}>
              {PHASE_OPTIONS.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
            </select>
          </div>
        )}

        {/* Results count */}
        {searched && !searching && (
          <div className={styles.resultCount}>
            {totalCount.toLocaleString()} studies found
          </div>
        )}

        {importError && (
          <div className={styles.importError}>{importError}</div>
        )}

        {/* Results list */}
        <div className={styles.resultsList}>
          {results.map((study) => {
            const isExpanded = expandedNct === study.nctId;
            const isImporting = importingNct === study.nctId;

            return (
              <div key={study.nctId} className={styles.studyCard}>
                <div className={styles.studyHeader} onClick={() => handleExpand(study.nctId)}>
                  <div className={styles.studyMain}>
                    <div className={styles.studyTitleRow}>
                      <span className={styles.nctId}>{study.nctId}</span>
                      <Badge variant={statusBadgeVariant(study.overallStatus)}>
                        {study.overallStatus.replace(/_/g, " ")}
                      </Badge>
                      {study.phase && <Badge variant="info">{study.phase.replace("PHASE", "Phase ")}</Badge>}
                    </div>
                    <div className={styles.studyTitle}>
                      {study.briefTitle}
                    </div>
                    <div className={styles.studyMeta}>
                      {study.conditions.length > 0 && (
                        <span><FlaskConical size={11} /> {study.conditions.slice(0, 3).join(", ")}</span>
                      )}
                      {study.sponsor && <span><Building2 size={11} /> {study.sponsor}</span>}
                      {study.enrollment && <span><Users size={11} /> {study.enrollment.toLocaleString()}</span>}
                      {study.startDate && <span><Calendar size={11} /> {study.startDate}</span>}
                    </div>
                  </div>
                  <div className={styles.studyActions}>
                    <button
                      className={styles.importBtn}
                      onClick={(e) => { e.stopPropagation(); handleImport(study.nctId); }}
                      disabled={isImporting}
                      aria-label={`Import ${study.nctId}`}
                    >
                      {isImporting ? <Loader2 size={14} className={styles.spin} /> : <Download size={14} />}
                      Import
                    </button>
                    {isExpanded ? <ChevronUp size={16} /> : <ChevronDown size={16} />}
                  </div>
                </div>

                {/* Expanded detail */}
                {isExpanded && (
                  <div className={styles.studyDetail}>
                    {loadingDetail ? (
                      <div className={styles.detailLoading}>
                        <Loader2 size={16} className={styles.spin} /> Loading details…
                      </div>
                    ) : studyDetail && studyDetail.nctId === study.nctId ? (
                      <>
                        {studyDetail.briefSummary && (
                          <div className={styles.detailSection}>
                            <div className={styles.detailLabel}>Summary</div>
                            <div className={styles.detailText}>{studyDetail.briefSummary.slice(0, 500)}{studyDetail.briefSummary.length > 500 ? "…" : ""}</div>
                          </div>
                        )}
                        {studyDetail.interventions.length > 0 && (
                          <div className={styles.detailSection}>
                            <div className={styles.detailLabel}>Interventions</div>
                            <div className={styles.detailChips}>
                              {studyDetail.interventions.map((iv, i) => <span key={i} className={styles.chip}>{iv}</span>)}
                            </div>
                          </div>
                        )}
                        {studyDetail.eligibilityCriteria && (
                          <div className={styles.detailSection}>
                            <div className={styles.detailLabel}>Eligibility Criteria</div>
                            <pre className={styles.criteriaText}>{studyDetail.eligibilityCriteria.slice(0, 800)}{studyDetail.eligibilityCriteria.length > 800 ? "\n…" : ""}</pre>
                          </div>
                        )}
                        <div className={styles.detailMeta}>
                          {studyDetail.minimumAge && <span>Min age: {studyDetail.minimumAge}</span>}
                          {studyDetail.maximumAge && <span>Max age: {studyDetail.maximumAge}</span>}
                          {studyDetail.sex && <span>Sex: {studyDetail.sex}</span>}
                        </div>
                        <a
                          href={`https://clinicaltrials.gov/study/${study.nctId}`}
                          target="_blank"
                          rel="noopener noreferrer"
                          className={styles.ctgLink}
                        >
                          <ExternalLink size={12} /> View on ClinicalTrials.gov
                        </a>
                      </>
                    ) : null}
                  </div>
                )}
              </div>
            );
          })}

          {results.length === 0 && searched && !searching && (
            <div className={styles.emptyResults}>No studies found. Try different search terms.</div>
          )}

          {!searched && (
            <div className={styles.emptyResults}>
              Search ClinicalTrials.gov to find and import trials into your system.
            </div>
          )}
        </div>

        {/* Load more */}
        {nextPageToken && (
          <button className={styles.loadMore} onClick={() => handleSearch(nextPageToken)} disabled={searching}>
            {searching ? <Loader2 size={14} className={styles.spin} /> : null}
            Load more results
          </button>
        )}
      </div>
    </Modal>
  );
}
