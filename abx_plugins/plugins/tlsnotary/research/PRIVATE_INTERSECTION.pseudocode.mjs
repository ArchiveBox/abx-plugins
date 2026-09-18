/**
 * Private capture -> authenticated AX intersection -> public evidence.
 *
 * DESIGN PSEUDOCODE, NOT AN IMPLEMENTATION OR AN EXISTING TLSNOTARY API.
 * Every `ideal.*` operation below is an unimplemented protocol requirement.
 * There are deliberately no substitute hashes, signatures, mock proofs, or
 * fallbacks that turn an unavailable proof into a successful verification.
 *
 * Deployment: ArchiveBox nodes run capture, verifier, and round workers under
 * supervisord, communicating over a tailnet. A node can perform all roles, but
 * its own verifier is not an independent witness to its captures. The network
 * coordinator is untrusted and never receives the original captures.
 *
 * Default use case: ten collectors privately intersect ten authenticated AX
 * trees, reconstruct the common extract locally, and approve its publication.
 * A single witnessed capture remains useful without participating in PSI.
 *
 * PUBLIC: approved artifact, final proof, policies, origin-witness identities,
 *         aggregate support claims, and timestamp evidence.
 * ROUND-PRIVATE: opaque scope, input commitments, scoped nullifiers, messages.
 * LOCAL ONLY: credentials, original responses, AX trees, source mappings,
 *             commitment openings, and unmatched content.
 *
 * Privacy of these protocol messages does not make tailnet IPs or traffic
 * timing anonymous. Protecting transport metadata is a separate requirement.
 */

export const POLICY = {
  version: "private-intersection-v0-design",
  capture: {
    originWitnessesPerCapture: 1,
    requireIndependentOperator: true,
    authenticateRequestResource: true, // Method + resource, without cookies.
    authenticateAllInputsNeededForReleasedClaims: true,
    acceptAuthenticatedErrorResponses: true, // Removal/availability evidence.
  },
  ax: {
    // These names specify desired contracts, not implemented algorithms.
    browserBuild: "PIN_EXACT_BUILD",
    captureSettings: "PIN_LANGUAGE_VIEWPORT_FRAMES_AND_CAPTURE_BOUNDARY",
    canonicalizer: "VERSIONED_AX_BLOCKS_WITH_CONTEXT_AND_ORDER",
    comparison: "complete-blocks-with-supported-structural-relationships",
    omitEphemeralNodeIdsFromComparison: true,
    retainSourceMappingsPrivately: true,
    ambiguousAlignment: "omit-with-explicit-omission",
    media: "require-separate-authenticated-bytes-not-just-accessible-labels",
  },
  intersection: {
    support: "all-contributors", // Later: explicit k-of-n, as a new policy.
    claim: "same-contributors-support-entire-released-artifact",
    completeness: "exact-output-of-the-declared-policy-over-frozen-inputs",
    reveal: "local-reconstruction-instructions-only",
  },
  publication: {
    renderer: "VERSIONED_INERT_EVIDENCE_VIEW",
    includeOriginalHTMLOrScripts: false,
    omissions: "fixed-marker-without-hidden-text-or-original-length",
    requireEveryContributorApproval: true,
    exposeCollectorIdentities: false,
  },
};

// 1. Runs on EACH collector's machine, independently of any later PSI round.
export async function capturePrivately({ owner, url, witnessRoster, ideal }) {
  const assignment = await ideal.assignIndependentWitness({
    owner,
    witnessRoster,
    count: POLICY.capture.originWitnessesPerCapture,
    selection: "unpredictable-assignment-with-recorded-reassignments",
    // Assignment excludes the owner's operator; keys alone do not prove
    // operator independence. Do not let clients grind for friendly witnesses.
  });

  const capture = await ideal.captureWithLiveTLSWitness({
    localBrowser: owner.browser,
    url, // Local browser input; not plaintext metadata sent to the witness.
    assignment,
    policy: POLICY.capture,
    // IDEAL REQUIREMENT: retain authenticated request/response transcripts,
    // receipts, openings, and the evidence needed to bind rendering to them.
    // TLSNotary participates DURING retrieval, never after an ordinary capture.
    // A failed session is not an authenticated capture. A verified HTTP 404
    // can be one; classification of its meaning is a separate statement.
  });

  const axSnapshot = await ideal.getAXSnapshot(capture.browserTarget);
  // CDP source: Accessibility.getFullAXTree for the required document frames.
  // Reading this target is not, by itself, a proof that its tree is authentic.

  const sealedEvidence = await ideal.commitAndSealLocally({
    owner,
    privateData: { capture, axSnapshot, policy: POLICY },
    // A hiding commitment; its random opening stays in encrypted local storage.
  });
  const captureTimestamp = await ideal.openTimestamps.stamp(
    sealedEvidence.commitment,
  );
  await ideal.storeTimestampLocally(sealedEvidence.localRef, captureTimestamp);
  // A calendar receipt may be pending. Upgrade and verify it asynchronously;
  // never label a pending receipt as Bitcoin-anchored.

  return sealedEvidence.localRef; // Local handle, never a peer download URL.
}

// 2. Establish a private round; freeze the actual accepted inputs in step 4.
export async function beginRound({
  scope,
  membershipPolicy,
  contributorCount = 10,
  ideal,
}) {
  return ideal.openPrivateRound({
    scope, // Opaque resource scope from private discovery, not hash(public URL).
    membershipPolicy, // Enrollment root + accepted origin operators/keys.
    expectedContributorCount: contributorCount,
    policy: POLICY,
    // Each proof must establish that its authenticated resource matches scope.
    // Scope discovery, enrollment, and anonymous transport are separate APIs.
    // One contribution per admitted credential, not proof of one human.
  });
}

// 3. EACH collector prepares and submits a proof-bound input, locally.
export async function joinRound({ owner, localRef, round, ideal }) {
  const original = await ideal.openLocalEvidence(owner, localRef);
  const ax = await ideal.canonicalizeAX(original.axSnapshot, round.policy.ax);
  // ax = { blocks, relationships, order, privateSourceMappings }.
  // Node IDs/offsets remain local. Different account-menu descendants must not
  // change the comparison identity of an otherwise identical article block.

  const committedAX = await ideal.commitPrivately(ax);
  const membership = await ideal.proveAnonymousMembership({
    privateCredential: owner.membershipCredential,
    round,
    // Produces a round-scoped nullifier to prevent duplicate contributions.
    // It must not be an unrestricted client-chosen pseudonym.
  });

  const inputProof = await ideal.proveAuthenticatedAXInput({
    publicStatement: {
      roundId: round.id,
      scope: round.scope,
      policy: round.policy,
      membershipRoot: round.membershipRoot,
      witnessRosterRoot: round.witnessRosterRoot,
      inputCommitment: committedAX.commitment,
      nullifier: membership.nullifier,
    },
    privateWitness: { original, ax, committedAX, membership },
    // MUST CHECK: authentic receipts under admitted keys; valid assignment;
    // request-resource binding; receipt commitments open to these responses;
    // AX content/structure derive from those responses under the declared
    // rendering/extraction relation; canonicalization is exact; membership
    // and nullifier are valid. Raw receipts and collector identities stay hidden.
    //
    // HARD UNIMPLEMENTED BRIDGE: authenticated responses -> rendered AX tree.
    // Hashing a CDP snapshot does not implement this relation. The current
    // plugin's separately replayed main response cannot authenticate arbitrary
    // tab state, scripts, API responses, media, or a whole browser execution.
  });

  await ideal.submitRoundInput(round, {
    inputCommitment: committedAX.commitment,
    nullifier: membership.nullifier,
    inputProof,
  });
  return { owner, round, ax, committedAX, membership, inputProof };
  // This returned state stays on THIS collector's machine, not a coordinator.
}

// 4. Called by EACH participant; calls rendezvous in one private computation.
export async function intersectPrivately({ localParticipant, ideal }) {
  const { owner, round, ax, committedAX, membership } = localParticipant;
  const frozenRound = await ideal.freezeAndAgreeRoundInputs(round);
  // All participants agree to one exact roster/commitment list and policy.
  // Require expectedContributorCount inputs; otherwise the round stays incomplete.
  // Reject duplicates/invalid proofs. No input changes after match disclosure.
  // An absent contributor is not a negative vote or a silent threshold change.

  const result = await ideal.committedMaliciousSecurePSI({
    frozenRound,
    localPrivateInput: { ax, committedAX, membership },
    // Consumes EXACTLY the inputs covered by the admission proofs. No swapping
    // arbitrary encrypted probe sets in after proving a different valid input.
    // Compute common blocks AND supported context/order, not a bag of words.
    // Keep unmatched values, raw hashes, mappings, and source receipts private.
    // Return each member only instructions for reconstructing from its OWN data.
    //
    // MUST ALSO PRODUCE a publicly verifiable proof binding the result to the
    // frozen inputs/policy. An MPC transcript or committee signature alone is
    // not this proof. Malicious behavior may abort; it must not release secrets.
  });

  const artifact = await ideal.renderLocally({
    ax,
    instructions: result.localReconstruction,
    renderer: frozenRound.policy.publication.renderer,
    // Only approved content + fixed inert markup, with explicit omissions.
    // No original scripts, attributes, source IDs, URLs, or asset hotlinks are
    // copied implicitly. Any published resource identifier is deliberate output.
  });
  await ideal.verifyArtifactOpening({
    artifact,
    opening: result.localArtifactOpening,
    commitment: result.artifactCommitment,
  });

  return { owner, frozenRound, artifact, result };
  // Artifact and opening are still local. No plaintext has been published.
}

// 5. EACH contributor reviews locally and authorizes this exact artifact.
export async function approvePublication({ localResult, ideal }) {
  const { owner, frozenRound, artifact, result } = localResult;
  const approved = await ideal.reviewLocally(owner, {
    artifact,
    disclosure: result.disclosureSummary,
    // Shared content can still identify a group. This is a disclosure decision,
    // not a claim that intersection mathematically eliminates every form of PII.
  });
  if (!approved) return { status: "withheld" };

  return ideal.authorizeAnonymously({
    privateCredential: owner.membershipCredential,
    statement: {
      frozenRoundCommitment: frozenRound.commitment,
      artifactCommitment: result.artifactCommitment,
      audience: "public",
    },
    // Bind approval to an actual contributor and prevent duplicate approvals.
    // Approval records are private inputs to the final proof, not a public list
    // of source keys. Missing approval does not silently become consent.
  });
}

// 6. One authorized publisher assembles the public package; anyone can verify.
export async function publishIntersection({ localResult, approvals, ideal }) {
  const { frozenRound, artifact, result } = localResult;
  const proof = await ideal.proveApprovedPublication({
    intersectionProof: result.proof,
    frozenRound,
    approvals,
    artifact,
    artifactOpening: result.localArtifactOpening,
    // CHECK: exact policy output, same contributors support the complete view,
    // unique enrollment credentials, all required approvals, and artifact bytes.
    // Publish distinct contributor count and distinct origin-operator count
    // separately. Repeated sessions under one operator are not new operators.
    // Hide source identities, scoped nullifiers, private receipt details, and
    // originals. Verify the underlying capture chain/timestamps inside the proof
    // when claimed; do not publish identifiable originals as a verification aid.
  });

  const packageToPublish = { artifact, statement: proof.statement, proof };
  const commitment = await ideal.commitPrivately(packageToPublish);
  const timestamp = await ideal.openTimestamps.stamp(commitment.commitment);
  const publication = {
    ...packageToPublish,
    evidenceCommitment: commitment.commitment,
    evidenceOpening: commitment.opening, // Opening this approved package only.
    timestamp,
  };
  await ideal.publish(publication);
  return publication;
}

// 7. Runs on a reviewer's device, without originals or access to collectors.
export async function verifyPublicEvidence({ publication, trustPolicy, ideal }) {
  // trustPolicy comes from the reviewer, NOT from keys supplied by the artifact.
  await ideal.verifyPublicationProof(publication, trustPolicy);
  await ideal.verifyEvidenceCommitment(publication);
  const timestamp = await ideal.openTimestamps.verify(publication);
  return { ...publication.statement, timestamp };
  // Report pending/anchored/invalid accurately. A valid proof does not prove
  // that the website's claims are true, nor that the public text is anonymous.
}

/**
 * IMPLEMENT GRADUALLY; DO NOT RELABEL A PARTIAL STAGE AS THE FULL GUARANTEE:
 *
 * A. Existing 1:1 witnessed responses + encrypted evidence storage + OTS.
 *    Add authenticated resource binding and deliberate error-response handling.
 * B. Capture/canonicalize real AX snapshots locally and render proposed extracts.
 *    At this stage AX is LOCAL OBSERVATION, not yet authenticated browser output.
 * C. Implement and validate the authenticated-response -> AX proof relation.
 * D. Implement committed private intersection and its portable output proof.
 * E. Add anonymous enrollment, exact-artifact approvals, export, offline checking.
 *
 * Scaling: a frozen artifact can accumulate new independently proved support
 * without changing its bytes. Each new collector authenticates its own capture
 * with another assigned witness and proves support against the fixed artifact.
 * That can target O(N) contributions for bounded artifacts; this sketch does not
 * claim a linear implementation of arbitrary private tree alignment. Recomputing
 * an intersection or changing an artifact creates a new round and approvals.
 * Later observations retain their own times, not the original capture's time.
 *
 * Source boundaries:
 * - ../README.md and EXTENSION_API_AUDIT.md describe what currently exists.
 * - https://tlsnotary.org/docs/faq/ (authenticated commitments -> later ZK).
 * - https://chromedevtools.github.io/devtools-protocol/tot/Accessibility/
 * - https://csrc.nist.gov/Projects/pec/psi (committed private set operations).
 * - https://github.com/opentimestamps/opentimestamps-client
 */
