/**
 * PHASE 1: TLSNotary capture -- independently witnessed, private source evidence.
 *
 * DESIGN PSEUDOCODE. Every `ideal.*` operation is an unimplemented contract,
 * not an existing TLSNotary API. No mock proofs or successful fallbacks.
 *
 * Vocabulary: collector, origin witness, TLS session, capture, receipt.
 * This phase has no PSI rounds, contributors, intersection, or publication.
 * It does not need to know whether the capture will ever be used in PSI.
 *
 * One collector fetches a resource while another operator witnesses the TLS
 * exchange. The collector preserves the private evidence and receives a local
 * captureRef. The origin witness sees protocol metadata, not credentials or
 * response plaintext. Its own verifier is not an independent origin witness.
 *
 * A sealed capture contains responses, receipts, secret commitment openings,
 * the capture policy, and local rendering observations/provenance material.
 * Observing an AX tree does NOT itself authenticate that rendered tree.
 *
 * captureRef is the boundary with downstream consumers: an existing evidence
 * record, not a browser session, PSI membership, or a peer download URL.
 */

export const TLSNOTARY_CAPTURE_POLICY = {
  version: "tlsnotary-capture-v0-design",
  originWitnessesPerCapture: 1,
  requireIndependentOperator: true,
  authenticateRequestResource: true, // Method + resource, without cookies.
  authenticateClaimedResources: true,
  acceptAuthenticatedErrorResponses: true, // Removal/availability evidence.
  browserBuild: "PIN_EXACT_BUILD",
  captureSettings: "PIN_LANGUAGE_VIEWPORT_FRAMES_AND_CAPTURE_BOUNDARY",
};

export async function createTLSNotaryCapture({
  owner,
  url,
  originWitnessRoster,
  ideal,
}) {
  const witnessAssignment = await ideal.assignTLSNotaryOriginWitness({
    owner,
    originWitnessRoster,
    count: TLSNOTARY_CAPTURE_POLICY.originWitnessesPerCapture,
    selection: "unpredictable-assignment-with-recorded-reassignments",
    // Exclude the owner's operator. Distinct keys do not establish independent
    // operators; clients must not grind assignments for a friendly witness.
  });

  const witnessedRetrieval = await ideal.captureWithLiveTLSWitness({
    localBrowser: owner.browser,
    url, // Local input, not plaintext metadata sent to the origin witness.
    witnessAssignment,
    policy: TLSNOTARY_CAPTURE_POLICY,
    // Retain authenticated request/response transcripts, receipts, openings,
    // and the evidence needed to bind rendering observations to those inputs.
    // TLSNotary participates DURING retrieval, never after an ordinary capture.
    // A failed TLS session is not authenticated evidence. A verified HTTP 404
    // can be; interpreting its meaning is a separate claim.
  });

  const axSnapshot = await ideal.getAXSnapshot(witnessedRetrieval.browserTarget);
  // CDP: Accessibility.getFullAXTree for the required document frames.
  // This is a LOCAL OBSERVATION, not yet a proof of authentic browser output.

  const sealedCapture = await ideal.commitAndSealTLSNotaryCaptureLocally({
    owner,
    privateData: {
      witnessedRetrieval,
      axSnapshot,
      policy: TLSNOTARY_CAPTURE_POLICY,
    },
    // Hiding commitment; its opening stays in encrypted local storage.
  });
  const captureTimestamp = await ideal.openTimestamps.stamp(
    sealedCapture.commitment,
  );
  await ideal.storeCaptureTimestamp(sealedCapture.captureRef, captureTimestamp);
  // Upgrade and verify pending OTS receipts asynchronously. Pending is not
  // Bitcoin-anchored, and OTS completion is separate from TLS authentication.

  return sealedCapture.captureRef;
}

// Standalone local verification: usable even if PSI is never performed.
export async function verifyTLSNotaryCapture({
  owner,
  captureRef,
  originTrustPolicy,
  ideal,
}) {
  const captureEvidence = await ideal.openLocalTLSNotaryCapture(owner, captureRef);
  const origin = await ideal.verifyTLSNotaryCaptureEvidence({
    captureEvidence,
    originTrustPolicy, // Independently configured, not supplied by the capture.
    // Check receipts, openings, resource binding, framing, and witness policy.
    // This verifies network evidence, not the attached AX observation.
  });
  const timestamp = await ideal.openTimestamps.verifyCapture(captureEvidence);
  return { origin, timestamp, axProvenance: "not-established-by-this-check" };
}

// Current implementation and gaps: ../README.md and EXTENSION_API_AUDIT.md.
// The existing plugin makes a separate main-response request; it does not yet
// authenticate the browser's complete network input or resulting AX tree.
