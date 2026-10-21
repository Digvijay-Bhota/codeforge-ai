/** Leave the app for another page (GitHub's sign-in). Separate so tests can observe it. */
export function navigateAway(url: string): void {
  window.location.assign(url);
}
