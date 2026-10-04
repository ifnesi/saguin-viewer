package viewer

import (
	"fmt"
	"math"
	"strconv"
	"strings"
)

// An expression is what a computed column's `value` holds: arithmetic and
// comparison over metric names and numbers.
//
// **What it deliberately cannot do.** No functions, no variables, no strings, no
// `and` or `or`. The readings RFC 0005 says the catalogue exists for are a
// subtraction and a comparison, and everything past that is a language rather
// than a column - the point at which a dashboard file stops being readable by
// somebody who did not write it. If `and` is ever needed it arrives with the
// failure that needed it, not on the grounds that a language usually has one.
//
// Operands are metric names. In a `rows` group each is resolved for the row
// being drawn; in a `columns` group each is the family's single value. **An
// operand the broker has no sample for makes the whole cell unknown**, which is
// drawn as a dash: a `latest` channel has no bytes and no consumer position, and
// reading either as zero would make a lag of nothing out of a lag nobody knows.

// Value is what an expression evaluates to. A comparison yields a boolean; a
// number that could not be worked out at all is not Known.
type Value struct {
	Num    float64
	IsBool bool
	Known  bool
}

// Expr is a parsed expression.
type Expr struct {
	root   node
	source string
	names  []string
}

// Metrics is every metric name the expression reads, in the order written. The
// loader holds each of them to the scrape exactly as it holds a bare column, so
// a typo inside an expression is refused at startup like any other.
func (e *Expr) Metrics() []string { return e.names }

// Source is the expression as written, for a message.
func (e *Expr) Source() string { return e.source }

// Eval works the expression out. resolve answers a metric name with its value
// for whatever is being drawn, and false where there is no sample.
func (e *Expr) Eval(resolve func(name string) (float64, bool)) Value {
	return e.root.eval(resolve)
}

type node interface {
	eval(func(string) (float64, bool)) Value
}

type numberNode float64

func (n numberNode) eval(func(string) (float64, bool)) Value {
	return Value{Num: float64(n), Known: true}
}

type metricNode string

func (m metricNode) eval(resolve func(string) (float64, bool)) Value {
	v, ok := resolve(string(m))
	return Value{Num: v, Known: ok}
}

type unaryNode struct {
	inner node
}

func (u unaryNode) eval(resolve func(string) (float64, bool)) Value {
	v := u.inner.eval(resolve)
	if !v.Known {
		return v
	}
	return Value{Num: -v.Num, Known: true}
}

type binaryNode struct {
	op    string
	left  node
	right node
}

func (b binaryNode) eval(resolve func(string) (float64, bool)) Value {
	l, r := b.left.eval(resolve), b.right.eval(resolve)
	// **Unknown spreads.** One operand the broker does not serve makes the answer
	// unknown rather than a number computed from a zero that was never measured.
	if !l.Known || !r.Known {
		return Value{}
	}
	switch b.op {
	case "+":
		return Value{Num: l.Num + r.Num, Known: true}
	case "-":
		return Value{Num: l.Num - r.Num, Known: true}
	case "*":
		return Value{Num: l.Num * r.Num, Known: true}
	case "/":
		// **Division by nothing is unknown, not infinity.** A ratio whose
		// denominator is zero is a question the scrape cannot answer - no
		// deliveries yet, no records yet - and a cell reading +Inf says the
		// opposite of "nothing has happened".
		if r.Num == 0 {
			return Value{}
		}
		q := l.Num / r.Num
		if math.IsNaN(q) || math.IsInf(q, 0) {
			return Value{}
		}
		return Value{Num: q, Known: true}
	}
	var yes bool
	switch b.op {
	case ">":
		yes = l.Num > r.Num
	case "<":
		yes = l.Num < r.Num
	case ">=":
		yes = l.Num >= r.Num
	case "<=":
		yes = l.Num <= r.Num
	case "==":
		yes = l.Num == r.Num
	case "!=":
		yes = l.Num != r.Num
	default:
		// Unreachable: the parser accepts no other operator.
		return Value{}
	}
	out := Value{IsBool: true, Known: true}
	if yes {
		out.Num = 1
	}
	return out
}

// ParseExpr reads an expression, or says what is wrong with it.
func ParseExpr(src string) (*Expr, error) {
	toks, err := lex(src)
	if err != nil {
		return nil, err
	}
	if len(toks) == 0 {
		return nil, fmt.Errorf("%q is empty", src)
	}
	p := &parser{toks: toks, src: src}
	root, err := p.comparison()
	if err != nil {
		return nil, err
	}
	if p.at < len(p.toks) {
		return nil, fmt.Errorf("%q: %q is left over after the expression ends",
			src, p.toks[p.at].text)
	}
	e := &Expr{root: root, source: src}
	seen := map[string]bool{}
	for _, t := range toks {
		if t.kind == tokMetric && !seen[t.text] {
			seen[t.text] = true
			e.names = append(e.names, t.text)
		}
	}
	if len(e.names) == 0 {
		return nil, fmt.Errorf("%q reads no metric, so it is the same number on "+
			"every screen", src)
	}
	return e, nil
}

type tokKind int

const (
	tokNumber tokKind = iota
	tokMetric
	tokOp
	tokOpen
	tokClose
)

type token struct {
	kind tokKind
	text string
	num  float64
}

func lex(src string) ([]token, error) {
	var out []token
	i := 0
	for i < len(src) {
		c := src[i]
		switch {
		case c == ' ' || c == '\t' || c == '\n':
			i++
		case c == '(':
			out = append(out, token{kind: tokOpen, text: "("})
			i++
		case c == ')':
			out = append(out, token{kind: tokClose, text: ")"})
			i++
		case strings.ContainsRune("+-*/", rune(c)):
			out = append(out, token{kind: tokOp, text: string(c)})
			i++
		case strings.ContainsRune("<>=!", rune(c)):
			op := string(c)
			if i+1 < len(src) && src[i+1] == '=' {
				op += "="
				i++
			}
			if op == "=" {
				return nil, fmt.Errorf("%q: a single `=` compares nothing - `==` "+
					"asks whether two numbers are equal", src)
			}
			if op == "!" {
				return nil, fmt.Errorf("%q: a bare `!` is not an operator here - "+
					"`!=` asks whether two numbers differ", src)
			}
			out = append(out, token{kind: tokOp, text: op})
			i++
		case c >= '0' && c <= '9' || c == '.':
			j := i
			for j < len(src) && (src[j] >= '0' && src[j] <= '9' || src[j] == '.') {
				j++
			}
			v, err := strconv.ParseFloat(src[i:j], 64)
			if err != nil {
				return nil, fmt.Errorf("%q: %q is not a number", src, src[i:j])
			}
			out = append(out, token{kind: tokNumber, text: src[i:j], num: v})
			i = j
		case c == '_' || c >= 'a' && c <= 'z' || c >= 'A' && c <= 'Z':
			j := i
			for j < len(src) && (src[j] == '_' || src[j] >= 'a' && src[j] <= 'z' ||
				src[j] >= 'A' && src[j] <= 'Z' || src[j] >= '0' && src[j] <= '9') {
				j++
			}
			out = append(out, token{kind: tokMetric, text: src[i:j]})
			i = j
		default:
			return nil, fmt.Errorf("%q: %q is not something this reads. An "+
				"expression is metric names and numbers with + - * / and a "+
				"comparison", src, string(c))
		}
	}
	return out, nil
}

type parser struct {
	toks []token
	at   int
	src  string
}

func (p *parser) peek() (token, bool) {
	if p.at >= len(p.toks) {
		return token{}, false
	}
	return p.toks[p.at], true
}

// comparison is the outermost level: at most one, because chaining them - `a < b
// < c` - reads as mathematics and means something else in every language that
// allows it.
func (p *parser) comparison() (node, error) {
	left, err := p.sum()
	if err != nil {
		return nil, err
	}
	t, ok := p.peek()
	if !ok || t.kind != tokOp || !isComparison(t.text) {
		return left, nil
	}
	p.at++
	right, err := p.sum()
	if err != nil {
		return nil, err
	}
	if next, ok := p.peek(); ok && next.kind == tokOp && isComparison(next.text) {
		return nil, fmt.Errorf("%q: two comparisons in one expression. `a %s b %s "+
			"c` reads as mathematics and is not what it does - write one", p.src,
			t.text, next.text)
	}
	return binaryNode{op: t.text, left: left, right: right}, nil
}

func isComparison(op string) bool {
	switch op {
	case ">", "<", ">=", "<=", "==", "!=":
		return true
	}
	return false
}

func (p *parser) sum() (node, error) {
	left, err := p.product()
	if err != nil {
		return nil, err
	}
	for {
		t, ok := p.peek()
		if !ok || t.kind != tokOp || (t.text != "+" && t.text != "-") {
			return left, nil
		}
		p.at++
		right, err := p.product()
		if err != nil {
			return nil, err
		}
		left = binaryNode{op: t.text, left: left, right: right}
	}
}

func (p *parser) product() (node, error) {
	left, err := p.unary()
	if err != nil {
		return nil, err
	}
	for {
		t, ok := p.peek()
		if !ok || t.kind != tokOp || (t.text != "*" && t.text != "/") {
			return left, nil
		}
		p.at++
		right, err := p.unary()
		if err != nil {
			return nil, err
		}
		left = binaryNode{op: t.text, left: left, right: right}
	}
}

func (p *parser) unary() (node, error) {
	if t, ok := p.peek(); ok && t.kind == tokOp && t.text == "-" {
		p.at++
		inner, err := p.unary()
		if err != nil {
			return nil, err
		}
		return unaryNode{inner: inner}, nil
	}
	return p.atom()
}

func (p *parser) atom() (node, error) {
	t, ok := p.peek()
	if !ok {
		return nil, fmt.Errorf("%q ends where a metric or a number should be",
			p.src)
	}
	switch t.kind {
	case tokNumber:
		p.at++
		return numberNode(t.num), nil
	case tokMetric:
		p.at++
		return metricNode(t.text), nil
	case tokOpen:
		p.at++
		inner, err := p.comparison()
		if err != nil {
			return nil, err
		}
		next, ok := p.peek()
		if !ok || next.kind != tokClose {
			return nil, fmt.Errorf("%q: a `(` that is never closed", p.src)
		}
		p.at++
		return inner, nil
	case tokClose:
		return nil, fmt.Errorf("%q: a `)` with no `(` before it", p.src)
	default:
		return nil, fmt.Errorf("%q: %q is where a metric or a number should be",
			p.src, t.text)
	}
}
