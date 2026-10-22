import { useJobsStore } from "../store/jobsStore";
import { confirmDelete } from "./confirmDelete";

export default function JobList() {
  const jobs = useJobsStore((s) => s.jobs);
  const loading = useJobsStore((s) => s.loading);
  const selectedId = useJobsStore((s) => s.selectedId);
  const select = useJobsStore((s) => s.select);
  const remove = useJobsStore((s) => s.remove);

  if (!loading && jobs.length === 0) {
    return <section className="list empty">No jobs here yet. Try “Search jobs”.</section>;
  }

  return (
    <section className="list">
      {jobs.map((j) => (
        // Two sibling buttons, not one inside the other: nested buttons are invalid HTML.
        <div key={j.id} className="card-row">
          <button className={`card ${j.id === selectedId ? "selected" : ""}`} onClick={() => select(j.id)}>
            <span className={`score ${scoreClass(j.match_score)}`}>{j.match_score ?? "–"}</span>
            <span className="meta">
              <strong>{j.title}</strong>
              <small>
                {j.company} · {j.location || "n/a"} · {j.portal}
              </small>
            </span>
          </button>
          {j.status !== "applied" && (
            <button
              className="icon danger"
              aria-label={`Delete ${j.title}`}
              title="Delete job"
              onClick={() => confirmDelete(j.title) && remove(j.id)}
            >
              ✕
            </button>
          )}
        </div>
      ))}
    </section>
  );
}

function scoreClass(score: number | null): string {
  if (score === null) return "none";
  return score >= 70 ? "good" : score >= 50 ? "mid" : "low";
}
