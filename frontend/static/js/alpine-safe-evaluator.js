/* CSP-safe Alpine expression evaluation.
 * Parses a deliberately small JavaScript expression grammar and evaluates it
 * against Alpine's data proxy. It never constructs functions at runtime.
 */
(function () {
  'use strict';

  const GLOBALS = Object.freeze({
    Array,
    Boolean,
    JSON,
    Math,
    Number,
    Object,
    String,
    decodeURIComponent,
    encodeURIComponent,
    isNaN,
    location,
    parseFloat,
    parseInt,
  });
  const FORBIDDEN_PROPERTIES = new Set([
    '__proto__',
    'apply',
    'bind',
    'call',
    'constructor',
    'eval',
    'prototype',
  ]);
  const PAGE_COMPONENTS = new Set([
    'adm', 'applyPage', 'detail', 'dg', 'gen', 'lst', 'mySchedules',
    'billingPage', 'nursesPage', 'prof', 'rolesPage', 'seq', 'shiftsPage',
    'skillsPage', 'sm', 'tenantApplicationsPage', 'tenantsPage', 'usersPage',
    'rulesTenantSelector',
  ]);
  const ASSIGNMENTS = new Map([
    ['=', (left, context, right) => left.set(context, right)],
    ['+=', (left, context, right) => left.set(context, left.get(context) + right)],
    ['-=', (left, context, right) => left.set(context, left.get(context) - right)],
    ['*=', (left, context, right) => left.set(context, left.get(context) * right)],
    ['/=', (left, context, right) => left.set(context, left.get(context) / right)],
    ['%=', (left, context, right) => left.set(context, left.get(context) % right)],
  ]);
  const PRECEDENCE = [
    ['??'],
    ['||'],
    ['&&'],
    ['===', '!==', '==', '!='],
    ['<=', '>=', '<', '>'],
    ['+', '-'],
    ['*', '/', '%'],
    ['**'],
  ];

  class ExpressionError extends Error {}

  class Parser {
    constructor(source) {
      this.source = source;
      this.pos = 0;
    }

    parse() {
      const expression = this.parseProgram();
      this.skipWhitespace();
      if (this.pos < this.source.length) {
        throw new ExpressionError(`Unexpected "${this.source.slice(this.pos)}"`);
      }
      return expression;
    }

    parseProgram() {
      const statements = [];
      for (;;) {
        this.skipWhitespace();
        while (this.accept(';')) this.skipWhitespace();
        if (this.pos >= this.source.length) break;
        statements.push(this.parseStatement());
        this.skipWhitespace();
        if (!this.accept(';')) break;
      }
      if (!statements.length) throw new ExpressionError('Empty expression');
      return (context) => {
        let value;
        for (const statement of statements) value = statement(context);
        return value;
      };
    }

    parseStatement() {
      this.skipWhitespace();
      if (this.accept('{')) return this.parseBlock();
      if (this.accept('if')) {
        this.skipWhitespace();
        this.expect('(');
        const condition = this.parseExpression();
        this.skipWhitespace();
        this.expect(')');
        const consequent = this.parseStatement();
        this.skipWhitespace();
        let alternate = () => undefined;
        if (this.accept('else')) alternate = this.parseStatement();
        return (context) => (condition(context) ? consequent(context) : alternate(context));
      }
      return this.parseExpression();
    }

    parseBlock() {
      const statements = [];
      this.skipWhitespace();
      while (!this.peek('}')) {
        if (this.accept(';')) {
          this.skipWhitespace();
          continue;
        }
        statements.push(this.parseStatement());
        this.skipWhitespace();
        this.accept(';');
        this.skipWhitespace();
      }
      this.expect('}');
      return (context) => {
        let value;
        for (const statement of statements) value = statement(context);
        return value;
      };
    }

    peek(text) {
      return this.source.startsWith(text, this.pos);
    }

    accept(text) {
      if (!this.peek(text)) return false;
      this.pos += text.length;
      return true;
    }

    acceptBinary(text) {
      if (!this.peek(text)) return false;
      const doubled = text === '+' || text === '-' || text === '*';
      if (doubled && this.peek(text.repeat(2))) return false;
      this.pos += text.length;
      return true;
    }

    expect(text) {
      if (!this.accept(text)) {
        throw new ExpressionError(`Expected "${text}" at ${this.pos}`);
      }
    }

    skipWhitespace() {
      const match = /^\s+/.exec(this.source.slice(this.pos));
      if (match) this.pos += match[0].length;
    }

    readIdentifier() {
      this.skipWhitespace();
      const match = /^[A-Za-z_$][A-Za-z0-9_$]*/.exec(this.source.slice(this.pos));
      if (!match) return null;
      this.pos += match[0].length;
      return match[0];
    }

    parseExpression() {
      const arrow = this.parseArrow();
      if (arrow) return arrow;
      return this.parseAssignment();
    }

    parseArrow() {
      const start = this.pos;
      const params = this.parseArrowParameters();
      if (!params) {
        this.pos = start;
        return null;
      }
      this.skipWhitespace();
      if (!this.accept('=>')) {
        this.pos = start;
        return null;
      }
      const body = this.parseChain(this.parseAssignment());
      return (context) => (...values) => {
        const arrowContext = Object.create(context);
        params.forEach((name, index) => {
          arrowContext[name] = values[index];
        });
        return body(arrowContext);
      };
    }

    parseArrowParameters() {
      this.skipWhitespace();
      const single = this.readIdentifier();
      if (single) return [single];
      if (!this.peek('(')) return null;

      const start = this.pos;
      let depth = 0;
      let quote = null;
      let escaped = false;
      for (let index = this.pos; index < this.source.length; index += 1) {
        const character = this.source[index];
        if (quote) {
          if (escaped) escaped = false;
          else if (character === '\\') escaped = true;
          else if (character === quote) quote = null;
          continue;
        }
        if (character === '"' || character === "'" || character === '`') quote = character;
        else if (character === '(') depth += 1;
        else if (character === ')') {
          depth -= 1;
          if (depth === 0) {
            const parameters = this.source.slice(start + 1, index);
            this.pos = index + 1;
            this.skipWhitespace();
            if (!this.peek('=>')) {
              this.pos = start;
              return null;
            }
            return parameters.split(',').map((parameter) => {
              const name = parameter.trim().replace(/=.*$/, '').trim();
              if (!/^[A-Za-z_$][A-Za-z0-9_$]*$/.test(name)) {
                throw new ExpressionError('Unsupported arrow parameter');
              }
              return name;
            });
          }
        }
      }
      return null;
    }

    parseAssignment() {
      const left = this.parseConditional();
      this.skipWhitespace();
      for (const [operator, apply] of ASSIGNMENTS) {
        if (!this.accept(operator)) continue;
        if (!left.assign) throw new ExpressionError('Invalid assignment target');
        const right = this.parseAssignment();
        return (context) => apply(left, context, right(context));
      }
      if (this.accept('++') || this.accept('--')) {
        if (!left.assign) throw new ExpressionError('Invalid update target');
        const increment = this.source[this.pos - 2] === '+';
        return (context) => {
          const previous = left.get(context);
          left.set(context, increment ? previous + 1 : previous - 1);
          return previous;
        };
      }
      return left;
    }

    parseConditional() {
      const test = this.parseBinary(0);
      this.skipWhitespace();
      if (!this.accept('?')) return test;
      const consequent = this.parseAssignment();
      this.skipWhitespace();
      this.expect(':');
      const alternate = this.parseAssignment();
      return (context) => (test(context) ? consequent(context) : alternate(context));
    }

    parseBinary(level) {
      if (level >= PRECEDENCE.length) return this.parseUnary();
      let left = this.parseBinary(level + 1);
      for (;;) {
        this.skipWhitespace();
        const operator = PRECEDENCE[level].find((candidate) => this.acceptBinary(candidate));
        if (!operator) return left;
        const right = this.parseBinary(level + 1);
        const previousLeft = left;
        if (operator === '||') {
          left = (context) => previousLeft(context) || right(context);
          continue;
        }
        if (operator === '&&') {
          left = (context) => previousLeft(context) && right(context);
          continue;
        }
        if (operator === '??') {
          left = (context) => previousLeft(context) ?? right(context);
          continue;
        }
        left = (context) => binary(operator, previousLeft(context), right(context));
      }
    }

    parseUnary() {
      this.skipWhitespace();
      if (this.accept('!')) {
        const value = this.parseUnary();
        return (context) => !value(context);
      }
      const negated = this.accept('-');
      if (negated || this.accept('+')) {
        const value = this.parseUnary();
        return (context) => (negated ? -value(context) : +value(context));
      }
      const value = this.parsePrimary();
      return this.parseChain(value);
    }

    parseChain(value) {
      for (;;) {
        this.skipWhitespace();
        if (this.accept('?.')) {
          const property = this.readIdentifier();
          if (!property) throw new ExpressionError('Expected optional property');
          value = createMember(value, property, true);
          continue;
        }
        if (this.accept('.')) {
          const property = this.readIdentifier();
          if (!property) throw new ExpressionError('Expected property');
          value = createMember(value, property, false);
          continue;
        }
        if (this.peek('[')) {
          this.expect('[');
          const property = this.parseExpression();
          this.skipWhitespace();
          this.expect(']');
          value = createComputedMember(value, property, false);
          continue;
        }
        if (this.peek('(')) {
          this.expect('(');
          const args = [];
          this.skipWhitespace();
          for (;;) {
            this.skipWhitespace();
            if (this.accept(')')) break;
            args.push(this.parseExpression());
            this.skipWhitespace();
            if (this.accept(',')) continue;
            this.expect(')');
            break;
          }
          const callee = value;
          value = (context) => {
            const target = callee.object ? callee.object(context) : null;
            const method = callee(context);
            if (typeof method !== 'function') throw new TypeError('Expression is not callable');
            return method.apply(target ?? context, args.map((argument) => argument(context)));
          };
          continue;
        }
        return value;
      }
    }

    parseUpdates(value) {
      this.skipWhitespace();
      if (!this.peek('++') && !this.peek('--')) return value;
      const increment = this.accept('++');
      this.accept('--');
      if (!value.assign) throw new ExpressionError('Invalid update target');
      return (context) => {
        const previous = value.get(context);
        value.set(context, increment ? previous + 1 : previous - 1);
        return previous;
      };
    }

    parsePrimary() {
      this.skipWhitespace();
      if (this.peek('(')) {
        this.expect('(');
        const value = this.parseExpression();
        this.skipWhitespace();
        this.expect(')');
        return value;
      }
      if (this.peek('[')) return this.parseArray();
      if (this.peek('{')) return this.parseObject();
      return this.parseLiteral();
    }

    parseArray() {
      this.expect('[');
      const values = [];
      this.skipWhitespace();
      while (!this.accept(']')) {
        values.push(this.parseExpression());
        this.skipWhitespace();
        if (!this.accept(',')) this.expect(']');
        this.skipWhitespace();
      }
      return (context) => values.map((value) => value(context));
    }

    parseObject() {
      this.expect('{');
      const properties = [];
      this.skipWhitespace();
      while (!this.accept('}')) {
        const key = this.parsePropertyKey();
        this.skipWhitespace();
        let value;
        if (this.accept(':')) value = this.parseAssignment();
        else value = (context) => context[key];
        properties.push([key, value]);
        this.skipWhitespace();
        if (!this.accept(',')) this.expect('}');
        this.skipWhitespace();
      }
      return (context) => Object.fromEntries(properties.map(([key, value]) => [key, value(context)]));
    }

    parsePropertyKey() {
      this.skipWhitespace();
      if (this.peek('"') || this.peek("'")) return this.parseString();
      const identifier = this.readIdentifier();
      if (!identifier) throw new ExpressionError('Expected object key');
      return identifier;
    }

    parseLiteral() {
      this.skipWhitespace();
      if (this.peek('"') || this.peek("'")) {
        const value = this.parseString();
        return () => value;
      }
      if (this.peek('`')) return this.parseTemplate();
      const match = /^(?:0[xXbBoO][0-9a-fA-F]+|\d+(?:\.\d*)?(?:[eE][+-]?\d+)?)\b/.exec(
        this.source.slice(this.pos),
      );
      if (match) {
        this.pos += match[0].length;
        const value = Number(match[0]);
        return () => value;
      }
      const identifier = this.readIdentifier();
      if (!identifier) {
        throw new ExpressionError(`Unexpected character at ${this.pos}`);
      }
      if (identifier === 'true') return () => true;
      if (identifier === 'false') return () => false;
      if (identifier === 'null') return () => null;
      if (identifier === 'undefined') return () => undefined;
      if (identifier === 'this') return (context) => context;
      const reference = (context) => context[identifier];
      reference.assign = (context, value) => {
        context[identifier] = value;
      };
      reference.get = (context) => context[identifier];
      reference.set = (context, value) => {
        context[identifier] = value;
      };
      return reference;
    }

    parseString() {
      const quote = this.source[this.pos];
      this.pos += 1;
      let value = '';
      while (this.pos < this.source.length) {
        const character = this.source[this.pos];
        this.pos += 1;
        if (character === quote) return value;
        if (character !== '\\') {
          value += character;
          continue;
        }
        const escaped = this.source[this.pos];
        this.pos += 1;
        const escapes = { n: '\n', r: '\r', t: '\t', b: '\b', f: '\f', v: '\v' };
        value += escapes[escaped] ?? escaped;
      }
      throw new ExpressionError('Unterminated string');
    }

    parseTemplate() {
      this.expect('`');
      const parts = [];
      let text = '';
      while (this.pos < this.source.length) {
        const character = this.source[this.pos];
        if (character === '`') {
          this.pos += 1;
          if (text) parts.push(text);
          return (context) => parts.map((part) => (
            typeof part === 'function' ? templateValue(part(context)) : part
          )).join('');
        }
        if (character === '\\') {
          this.pos += 1;
          text += this.source[this.pos];
          this.pos += 1;
          continue;
        }
        if (character !== '$' || this.source[this.pos + 1] !== '{') {
          text += character;
          this.pos += 1;
          continue;
        }
        if (text) parts.push(text);
        text = '';
        this.pos += 2;
        const expression = this.readBalanced('{', '}');
        parts.push(new Parser(expression).parse());
      }
      throw new ExpressionError('Unterminated template literal');
    }

    readBalanced(open, close) {
      const start = this.pos;
      let depth = 0;
      let quote = null;
      if (this.peek(open)) depth = 1;
      while (this.pos < this.source.length) {
        const character = this.source[this.pos];
        if (quote) {
          if (character === '\\') this.pos += 1;
          else if (character === quote) quote = null;
        } else if (character === '"' || character === "'" || character === '`') {
          quote = character;
        } else if (character === open) {
          depth += 1;
        } else if (character === close) {
          depth -= 1;
          if (depth <= 0) {
            const value = this.source.slice(start, this.pos);
            this.pos += 1;
            return value;
          }
        }
        this.pos += 1;
      }
      throw new ExpressionError(`Unterminated ${open}`);
    }
  }

  function binary(operator, left, right) {
    switch (operator) {
      case '??': return left ?? right;
      case '||': return left || right;
      case '&&': return left && right;
      case '|': return left | right;
      case '^': return left ^ right;
      case '&': return left & right;
      case '===': return left === right;
      case '!==': return left !== right;
      case '==': return left == right;
      case '!=': return left != right;
      case '<': return left < right;
      case '>': return left > right;
      case '<=': return left <= right;
      case '>=': return left >= right;
      case '<<': return left << right;
      case '>>': return left >> right;
      case '>>>': return left >>> right;
      case '+': return left + right;
      case '-': return left - right;
      case '*': return left * right;
      case '/': return left / right;
      case '%': return left % right;
      case '**': return left ** right;
      default: throw new ExpressionError(`Unsupported operator ${operator}`);
    }
  }

  function member(object, property, optional) {
    if (object == null && optional) return undefined;
    if (object == null) throw new TypeError(`Cannot read ${property} of ${object}`);
    if (FORBIDDEN_PROPERTIES.has(property)) throw new ExpressionError(`Forbidden property ${property}`);
    return object[property];
  }

  function createMember(object, property, optional) {
    const evaluate = (context) => member(object(context), property, optional);
    evaluate.object = object;
    evaluate.get = evaluate;
    evaluate.set = (context, value) => {
      const target = object(context);
      if (target == null) throw new TypeError(`Cannot set ${property} of ${target}`);
      if (FORBIDDEN_PROPERTIES.has(property)) throw new ExpressionError(`Forbidden property ${property}`);
      target[property] = value;
    };
    evaluate.assign = evaluate.set;
    return evaluate;
  }

  function createComputedMember(object, property, optional) {
    const evaluate = (context) => member(object(context), property(context), optional);
    evaluate.object = object;
    evaluate.get = evaluate;
    evaluate.set = (context, value) => {
      const target = object(context);
      const key = property(context);
      if (target == null) throw new TypeError(`Cannot set ${key} of ${target}`);
      target[key] = value;
    };
    evaluate.assign = evaluate.set;
    return evaluate;
  }

  function templateValue(value) {
    if (Array.isArray(value)) return value.join(',');
    if (value !== null && typeof value === 'object') return JSON.stringify(value);
    return String(value);
  }

  function buildContext(element, dynamic) {
    const data = window.Alpine.$data(element);
    const target = { ...dynamic };
    return new Proxy(target, {
      has: () => true,
      get(_, key) {
        if (key in target) return target[key];
        if (key in data) return data[key];
        if (key in GLOBALS) return GLOBALS[key];
        if (PAGE_COMPONENTS.has(key) && typeof window[key] === 'function') return window[key];
        return undefined;
      },
      set(_, key, value) {
        if (key in data) data[key] = value;
        else target[key] = value;
        return true;
      },
    });
  }

  function evaluate(element, expression) {
    const evaluateExpression = typeof expression === 'function'
      ? (context) => expression.call(context)
      : new Parser(expression).parse();
    return (receive = () => {}, options = {}) => {
      const context = buildContext(element, options.scope ?? {});
      try {
        let value = evaluateExpression(context);
        if (typeof value === 'function') value = value.apply(context, options.params ?? []);
        receive(value, context, options.params ?? []);
      } catch (error) {
        console.warn('[Alpine CSP]', expression, error);
        throw error;
      }
    };
  }

  window.NurseSafeExpressions = {
    evaluate,
    parse(source) {
      return new Parser(source).parse();
    },
  };

  document.addEventListener('alpine:init', () => {
    window.Alpine.setEvaluator(window.NurseSafeExpressions.evaluate);
  });
}());
