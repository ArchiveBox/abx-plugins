/**
 * PHASE 2: PSI -- private intersection of existing capture evidence.
 *
 * Input: completed local captureRef records from TLSNOTARY_CAPTURE.pseudocode.mjs.
 * Output: an approved public artifact and proof of authenticated intersection.
 * This phase never fetches a URL, selects an origin witness, or creates a TLS
 * session. One existing capture can support multiple separately authorized PSI
 * rounds; a PSI failure never invalidates or modifies the source capture.
 *
 * Vocabulary: PSI contributor, PSI round, PSI input, intersection, publication.
 * "Round" always means PSI; it never means TLSNotary capture or witnessing.
 *
 * DESIGN PSEUDOCODE, NOT AN IMPLEMENTATION OR AN EXISTING TLSNOTARY API.
 * Every `ideal.*` operation below is an unimplemented protocol requirement.
 * There are deliberately no substitute hashes, signatures, mock proofs, or
 * fallbacks that turn an unavailable proof into a successful verification.
 *
 * Deployment: a PSI worker consumes the local capture store and communicates
 * over the tailnet. The PSI coordinator is untrusted and receives no originals.
 *
 * Default use case: ten collectors privately intersect ten authenticated AX
 * trees, reconstruct the common extract locally, and approve its publication.
 * A single witnessed capture remains useful without participating in PSI.
 *
 * PUBLIC: approved artifact, final proof, policies, origin-witness identities,
 *         aggregate support claims, and timestamp evidence.
 * PSI-ROUND-PRIVATE: opaque scope, input commitments, scoped nullifiers, messages.
 * LOCAL ONLY: credentials, original responses, AX trees, source mappings,
 *             commitment openings, and unmatched content.
 *
 * Privacy of these protocol messages does not make tailnet IPs or traffic
 * timing anonymous. Protecting transport metadata is a separate requirement.
 */

export const PSI_POLICY = {
  version: "private-intersection-v0-design",
  ax: {
    // These names specify desired contracts, not implemented algorithms.
    acceptedCaptureProfile: "PIN_COMPATIBLE_CAPTURE_BUILDS_AND_SETTINGS",
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

// PSI 1. Establish a PSI round; freeze its accepted inputs in PSI 3.
export async function beginPSIRound({
  scope,
  membershipPolicy,
  contributorCount = 10,
  ideal,
}) {
  return ideal.openPSIRound({
    scope, // Opaque resource scope from private discovery, not hash(public URL).
    membershipPolicy, // Enrollment root + accepted origin operators/keys.
    expectedContributorCount: contributorCount,
    policy: PSI_POLICY,
    // Each proof must establish that its authenticated resource matches scope.
    // Scope discovery, enrollment, and anonymous transport are separate APIs.
    // One contribution per admitted credential, not proof of one human.
  });
}

// PSI 2. EACH contributor selects existing evidence and prepares a bound input.
export async function joinPSIRound({ owner, captureRef, psiRound, ideal }) {
  const captureEvidence = await ideal.openLocalTLSNotaryCapture(owner, captureRef);
  const ax = await ideal.canonicalizeAX(
    captureEvidence.axSnapshot,
    psiRound.policy.ax,
  );
  // ax = { blocks, relationships, order, privateSourceMappings }.
  // Node IDs/offsets remain local. Different account-menu descendants must not
  // change the comparison identity of an otherwise identical article block.

  const committedAX = await ideal.commitPrivately(ax);
  const membership = await ideal.proveAnonymousMembership({
    privateCredential: owner.membershipCredential,
    psiRound,
    // Produces a round-scoped nullifier to prevent duplicate contributions.
    // It must not be an unrestricted client-chosen pseudonym.
  });

  const psiInputProof = await ideal.provePSIInputFromCapture({
    publicStatement: {
      psiRoundId: psiRound.id,
      scope: psiRound.scope,
      policy: psiRound.policy,
      membershipRoot: psiRound.membershipRoot,
      acceptedOriginWitnessesRoot: psiRound.acceptedOriginWitnessesRoot,
      inputCommitment: committedAX.commitment,
      nullifier: membership.nullifier,
    },
    privateWitness: { captureEvidence, ax, committedAX, membership },
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

  await ideal.submitPSIInput(psiRound, {
    inputCommitment: committedAX.commitment,
    nullifier: membership.nullifier,
    psiInputProof,
  });
  return { owner, psiRound, ax, committedAX, membership, psiInputProof };
  // This returned state stays on THIS collector's machine, not a coordinator.
}

// PSI 3. Called by EACH contributor; calls rendezvous in one private computation.
export async function computePSIIntersection({ psiParticipant, ideal }) {
  const { owner, psiRound, ax, committedAX, membership } = psiParticipant;
  const frozenPSIRound = await ideal.freezeAndAgreePSIInputs(psiRound);
  // All participants agree to one exact roster/commitment list and policy.
  // Require expectedContributorCount inputs; otherwise the round stays incomplete.
  // Reject duplicates/invalid proofs. No input changes after match disclosure.
  // An absent contributor is not a negative vote or a silent threshold change.

  const psiResult = await ideal.committedMaliciousSecurePSI({
    frozenPSIRound,
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
    instructions: psiResult.localReconstruction,
    renderer: frozenPSIRound.policy.publication.renderer,
    // Only approved content + fixed inert markup, with explicit omissions.
    // No original scripts, attributes, source IDs, URLs, or asset hotlinks are
    // copied implicitly. Any published resource identifier is deliberate output.
  });
  await ideal.verifyArtifactOpening({
    artifact,
    opening: psiResult.localArtifactOpening,
    commitment: psiResult.artifactCommitment,
  });

  return { owner, frozenPSIRound, artifact, psiResult };
  // Artifact and opening are still local. No plaintext has been published.
}

// PSI 4. EACH contributor reviews locally and authorizes this exact artifact.
export async function approvePSIPublication({ localPSIResult, ideal }) {
  const { owner, frozenPSIRound, artifact, psiResult } = localPSIResult;
  const approved = await ideal.reviewLocally(owner, {
    artifact,
    disclosure: psiResult.disclosureSummary,
    // Shared content can still identify a group. This is a disclosure decision,
    // not a claim that intersection mathematically eliminates every form of PII.
  });
  if (!approved) return { status: "withheld" };

  return ideal.authorizeAnonymously({
    privateCredential: owner.membershipCredential,
    statement: {
      frozenPSIRoundCommitment: frozenPSIRound.commitment,
      artifactCommitment: psiResult.artifactCommitment,
      audience: "public",
    },
    // Bind approval to an actual contributor and prevent duplicate approvals.
    // Approval records are private inputs to the final proof, not a public list
    // of source keys. Missing approval does not silently become consent.
  });
}

// PSI 5. One authorized publisher assembles the artifact and its evidence.
export async function publishPSIArtifact({ localPSIResult, approvals, ideal }) {
  const { frozenPSIRound, artifact, psiResult } = localPSIResult;
  const proof = await ideal.proveApprovedPSIPublication({
    intersectionProof: psiResult.proof,
    frozenPSIRound,
    approvals,
    artifact,
    artifactOpening: psiResult.localArtifactOpening,
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

// PSI 6. Independent verification without originals or access to collectors.
export async function verifyPSIPublication({ publication, trustPolicy, ideal }) {
  // trustPolicy comes from the reviewer, NOT from keys supplied by the artifact.
  await ideal.verifyPSIPublicationProof(publication, trustPolicy);
  await ideal.verifyEvidenceCommitment(publication);
  const timestamp = await ideal.openTimestamps.verify(publication);
  return { ...publication.statement, timestamp };
  // Report pending/anchored/invalid accurately. A valid proof does not prove
  // that the website's claims are true, nor that the public text is anonymous.
}

/**
 * IMPLEMENT GRADUALLY; DO NOT RELABEL A PARTIAL STAGE AS THE FULL GUARANTEE:
 *
 * Phase 1 lives in TLSNOTARY_CAPTURE.pseudocode.mjs and can ship independently.
 * Its boundary is a sealed captureRef; none of the steps below initiate capture.
 *
 * A. Consume existing captures and canonicalize their local AX observations.
 * B. Render proposed extracts from those observations.
 *    At this stage AX is LOCAL OBSERVATION, not yet authenticated browser output.
 * C. Implement and validate the authenticated-response -> AX proof relation.
 * D. Implement committed private intersection and its portable output proof.
 * E. Add anonymous enrollment, exact-artifact approvals, export, offline checking.
 *
 * Scaling: a frozen artifact can accumulate new independently proved support
 * without changing its bytes. Each new collector authenticates its own capture
 * in phase 1, then proves support against the fixed artifact in phase 2.
 * That can target O(N) contributions for bounded artifacts; this sketch does not
 * claim a linear implementation of arbitrary private tree alignment. Recomputing
 * an intersection or changing an artifact creates a new PSI round and approvals.
 * Later observations retain their own times, not the original capture's time.
 *
 * Source boundaries:
 * - ../README.md and EXTENSION_API_AUDIT.md describe what currently exists.
 * - https://tlsnotary.org/docs/faq/ (authenticated commitments -> later ZK).
 * - https://chromedevtools.github.io/devtools-protocol/tot/Accessibility/
 * - https://csrc.nist.gov/Projects/pec/psi (committed private set operations).
 * - https://github.com/opentimestamps/opentimestamps-client
 */
