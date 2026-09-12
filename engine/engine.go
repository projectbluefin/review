// Package engine implements the deterministic first-slice Review engine.
package engine

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"regexp"
	"sync"
	"time"
)

const (
	ProtocolMajor = 1
	ProtocolMinor = 0
	EngineVersion = "0.1.0"
)

type State string

const (
	Accepted   State = "ACCEPTED"
	Preparing  State = "PREPARING"
	Running    State = "RUNNING"
	Stopping   State = "STOPPING"
	Finalizing State = "FINALIZING"
	Succeeded  State = "SUCCEEDED"
	Failed     State = "FAILED"
	Cancelled  State = "CANCELLED"
	TimedOut   State = "TIMED_OUT"
)

func (s State) terminal() bool {
	return s == Succeeded || s == Failed || s == Cancelled || s == TimedOut
}

var (
	ErrNotFound             = errors.New("job not found")
	ErrRequestConflict      = errors.New("request id is already bound to a different spec")
	ErrIncompatibleProtocol = errors.New("incompatible protocol major")
	ErrUnsupportedExecutor  = errors.New("unsupported executor")
)

type Version struct{ Major, Minor uint32 }
type Limits struct {
	Runtime     time.Duration
	OutputBytes uint64
}
type CredentialHandle struct{ Handle, Purpose string }
type Spec struct {
	LogicalReviewRunID string
	WorkloadKind       string
	ExecutorID         string
	Repository         string
	SourceRevision     string
	Image              string
	Harness            string
	Model              string
	Effort             string
	NetworkPolicy      string
	Limits             Limits
	Credentials        []CredentialHandle
	ClientProtocol     Version
}
type Event struct {
	JobID                             string
	Sequence                          uint64
	At                                time.Time
	Elapsed                           time.Duration
	Kind                              string
	State                             State
	Payload                           []byte
	SourceRevision, Image, ExecutorID string
}
type Receipt struct {
	JobID, RequestID, LogicalReviewRunID, Repository, SourceRevision, Image, ExecutorID string
	TerminalState                                                                       State
	Reason, Cleanup, EngineVersion                                                      string
	Protocol                                                                            Version
	StartedAt, EndedAt                                                                  time.Time
	OutputTruncated                                                                     bool
}
type Artifact struct {
	Name, MediaType, DigestSHA256, TrustClass string
	Size                                      uint64
	Complete                                  bool
}
type Snapshot struct {
	JobID        string
	State        State
	LastSequence uint64
	Receipt      *Receipt
}
type Capabilities struct {
	EngineVersion    string
	Protocol         Version
	Executors        []string
	MaxOutputBytes   uint64
	RuntimeAvailable bool
}

type Executor interface {
	Run(context.Context, func(string, []byte)) error
}
type FixtureExecutor struct{ Block bool }

func (f FixtureExecutor) Run(ctx context.Context, emit func(string, []byte)) error {
	emit("stdout", []byte("fixture started\n"))
	if f.Block {
		<-ctx.Done()
		return ctx.Err()
	}
	emit("stdout", []byte("fixture complete\n"))
	return nil
}

type job struct {
	requestID       string
	spec            Spec
	canonical       [32]byte
	state           State
	events          []Event
	receipt         *Receipt
	outputTruncated bool
	ctx             context.Context
	cancel          context.CancelFunc
	started         time.Time
	done            chan struct{}
	cancelRequested bool
}
type Engine struct {
	mu        sync.Mutex
	jobs      map[string]*job
	requests  map[string]string
	executors map[string]Executor
	maxOutput uint64
	now       func() time.Time
}

func New() *Engine {
	return &Engine{jobs: map[string]*job{}, requests: map[string]string{}, executors: map[string]Executor{"fixture/noop": FixtureExecutor{}, "fixture/blocking": FixtureExecutor{Block: true}}, maxOutput: 64 * 1024, now: time.Now}
}
func (e *Engine) Capabilities(client Version) (Capabilities, error) {
	if client.Major != ProtocolMajor {
		return Capabilities{}, ErrIncompatibleProtocol
	}
	return Capabilities{EngineVersion, Version{ProtocolMajor, ProtocolMinor}, []string{"fixture/noop", "fixture/blocking"}, e.maxOutput, false}, nil
}

func canonical(spec Spec) ([32]byte, error) {
	value, err := json.Marshal(spec)
	if err != nil {
		return [32]byte{}, err
	}
	return sha256.Sum256(value), nil
}
func (e *Engine) Start(requestID string, spec Spec) (string, bool, error) {
	if requestID == "" || spec.Repository == "" || spec.SourceRevision == "" || spec.Image == "" {
		return "", false, errors.New("request id and exact source/image identity are required")
	}
	if !regexp.MustCompile(`^[0-9a-f]{40}$`).MatchString(spec.SourceRevision) {
		return "", false, errors.New("source revision must be an exact 40-character lowercase commit")
	}
	if !regexp.MustCompile(`@sha256:[0-9a-f]{64}$`).MatchString(spec.Image) {
		return "", false, errors.New("image must be an immutable sha256 reference")
	}
	if spec.ClientProtocol.Major != ProtocolMajor {
		return "", false, ErrIncompatibleProtocol
	}
	if _, ok := e.executors[spec.ExecutorID]; !ok {
		return "", false, ErrUnsupportedExecutor
	}
	hash, err := canonical(spec)
	if err != nil {
		return "", false, err
	}
	e.mu.Lock()
	if id, ok := e.requests[requestID]; ok {
		j := e.jobs[id]
		e.mu.Unlock()
		if j.canonical != hash {
			return "", false, ErrRequestConflict
		}
		return id, true, nil
	}
	id := hex.EncodeToString(hash[:16])
	if _, exists := e.jobs[id]; exists {
		id = fmt.Sprintf("%s-%d", id, len(e.jobs)+1)
	}
	ctx, cancel := context.WithCancel(context.Background())
	j := &job{requestID: requestID, spec: spec, canonical: hash, state: Accepted, ctx: ctx, cancel: cancel, started: e.now(), done: make(chan struct{})}
	e.jobs[id] = j
	e.requests[requestID] = id
	e.emitLocked(id, j, "state", Accepted, nil)
	e.mu.Unlock()
	go e.run(id, j)
	return id, false, nil
}
func (e *Engine) emitLocked(id string, j *job, kind string, state State, payload []byte) {
	j.state = state
	j.events = append(j.events, Event{id, uint64(len(j.events) + 1), e.now(), e.now().Sub(j.started), kind, state, append([]byte(nil), payload...), j.spec.SourceRevision, j.spec.Image, j.spec.ExecutorID})
}
func (e *Engine) run(id string, j *job) {
	e.mu.Lock()
	e.emitLocked(id, j, "state", Preparing, nil)
	e.emitLocked(id, j, "state", Running, nil)
	e.mu.Unlock()
	ctx := j.ctx
	var deadline context.CancelFunc
	if j.spec.Limits.Runtime > 0 {
		ctx, deadline = context.WithTimeout(ctx, j.spec.Limits.Runtime)
		defer deadline()
	}
	err := e.executors[j.spec.ExecutorID].Run(ctx, func(kind string, payload []byte) {
		e.mu.Lock()
		defer e.mu.Unlock()
		limit := j.spec.Limits.OutputBytes
		if limit == 0 || limit > e.maxOutput {
			limit = e.maxOutput
		}
		used := uint64(0)
		for _, v := range j.events {
			used += uint64(len(v.Payload))
		}
		if used >= limit {
			return
		}
		if uint64(len(payload)) > limit-used {
			payload = payload[:limit-used]
			j.outputTruncated = true
			e.emitLocked(id, j, "output_truncated", j.state, nil)
		}
		e.emitLocked(id, j, kind, j.state, payload)
	})
	e.mu.Lock()
	defer e.mu.Unlock()
	final := Succeeded
	reason := "fixture completed"
	if errors.Is(err, context.DeadlineExceeded) {
		final = TimedOut
		reason = "runtime deadline exceeded"
	} else if j.cancelRequested || errors.Is(err, context.Canceled) {
		final = Cancelled
		reason = "cancelled by client"
	} else if err != nil {
		final = Failed
		reason = err.Error()
	}
	e.emitLocked(id, j, "state", Finalizing, nil)
	receipt := &Receipt{id, j.requestID, j.spec.LogicalReviewRunID, j.spec.Repository, j.spec.SourceRevision, j.spec.Image, j.spec.ExecutorID, final, reason, "COMPLETE", EngineVersion, Version{ProtocolMajor, ProtocolMinor}, j.started, e.now(), j.outputTruncated}
	j.receipt = receipt
	e.emitLocked(id, j, "terminal_receipt", final, nil)
	close(j.done)
}
func (e *Engine) Cancel(id string) (State, error) {
	e.mu.Lock()
	defer e.mu.Unlock()
	j, ok := e.jobs[id]
	if !ok {
		return "", ErrNotFound
	}
	if j.state.terminal() {
		return j.state, nil
	}
	if !j.cancelRequested {
		j.cancelRequested = true
		e.emitLocked(id, j, "cancellation", Stopping, nil)
		j.cancel()
	}
	return j.state, nil
}
func (e *Engine) Get(id string) (Snapshot, error) {
	e.mu.Lock()
	defer e.mu.Unlock()
	j, ok := e.jobs[id]
	if !ok {
		return Snapshot{}, ErrNotFound
	}
	var r *Receipt
	if j.receipt != nil {
		copy := *j.receipt
		r = &copy
	}
	return Snapshot{id, j.state, uint64(len(j.events)), r}, nil
}
func (e *Engine) Watch(id string, after uint64) ([]Event, error) {
	e.mu.Lock()
	defer e.mu.Unlock()
	j, ok := e.jobs[id]
	if !ok {
		return nil, ErrNotFound
	}
	if after >= uint64(len(j.events)) {
		return []Event{}, nil
	}
	out := make([]Event, len(j.events)-int(after))
	copy(out, j.events[after:])
	return out, nil
}
func (e *Engine) Wait(ctx context.Context, id string) error {
	e.mu.Lock()
	j, ok := e.jobs[id]
	e.mu.Unlock()
	if !ok {
		return ErrNotFound
	}
	select {
	case <-j.done:
		return nil
	case <-ctx.Done():
		return ctx.Err()
	}
}

// ListArtifacts returns an immutable snapshot. Fixture executors produce none.
func (e *Engine) ListArtifacts(id string) ([]Artifact, error) {
	e.mu.Lock()
	defer e.mu.Unlock()
	if _, ok := e.jobs[id]; !ok {
		return nil, ErrNotFound
	}
	return []Artifact{}, nil
}

// ReadArtifact fails closed because the first registered executors declare no artifacts.
func (e *Engine) ReadArtifact(id, name string, offset, maxBytes uint64) ([]byte, bool, error) {
	e.mu.Lock()
	defer e.mu.Unlock()
	if _, ok := e.jobs[id]; !ok {
		return nil, false, ErrNotFound
	}
	return nil, false, fmt.Errorf("artifact %q: %w", name, ErrNotFound)
}
