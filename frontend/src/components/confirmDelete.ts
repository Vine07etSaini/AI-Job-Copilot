// Deleting can't be undone and also drops the de-duplication record, so the job may come back in a later search.
export function confirmDelete(title: string): boolean {
  return window.confirm(`Delete "${title}" and its tailored resume?\nIt can show up again in a later search.`);
}
