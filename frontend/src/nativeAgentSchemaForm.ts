type JsonObject = Record<string, unknown>;

const NATIVE_AGENT_FIELDS = [
  "name",
  "system_prompt",
  "context_config",
  "react_config",
  "invite_config",
] as const;

export type NativeAgentFieldValue = string | number | boolean | null;

export interface NativeAgentFormField {
  key: string;
  path: string[];
  title: string;
  description?: string;
  kind: "string" | "number" | "integer" | "boolean";
  textarea: boolean;
  minimum?: number;
  maximum?: number;
}

export interface NativeAgentFormSection {
  key: string;
  title: string;
  description?: string;
  fields: NativeAgentFormField[];
}

export interface NativeAgentFormDefinition {
  fields: NativeAgentFormField[];
  sections: NativeAgentFormSection[];
  initialValue: JsonObject;
}

export function deriveNativeAgentForm(schemaValue: unknown): NativeAgentFormDefinition {
  const schema = requireObject(schemaValue, "AgentData schema");
  const properties = requireObject(schema.properties, "AgentData properties");
  const actualFields = Object.keys(properties).sort();
  const expectedFields = [...NATIVE_AGENT_FIELDS].sort();
  if (actualFields.join("\0") !== expectedFields.join("\0")) {
    throw new Error("AgentScope AgentData 可编辑字段已变化，请先完成映射审查。");
  }

  const fields: NativeAgentFormField[] = [];
  const sections: NativeAgentFormSection[] = [];
  for (const key of NATIVE_AGENT_FIELDS) {
    const fieldSchema = resolveSchema(schema, properties[key], `AgentData.${key}`);
    if (schemaKind(fieldSchema) === "object") {
      const nestedProperties = requireObject(fieldSchema.properties, `AgentData.${key} properties`);
      sections.push({
        key,
        title: schemaTitle(fieldSchema, key),
        description: optionalString(fieldSchema.description),
        fields: Object.entries(nestedProperties).map(([nestedKey, value]) => (
          scalarField(schema, value, [key, nestedKey])
        )),
      });
    } else {
      fields.push(scalarField(schema, fieldSchema, [key]));
    }
  }
  return { fields, sections, initialValue: objectDefaults(schema, schema) };
}

export function updateNativeAgentFormValue(
  current: JsonObject,
  path: string[],
  value: NativeAgentFieldValue,
): JsonObject {
  if (!path.length) return current;
  const [head, ...rest] = path;
  if (!rest.length) return { ...current, [head]: value };
  const nested = isObject(current[head]) ? current[head] : {};
  return { ...current, [head]: updateNativeAgentFormValue(nested, rest, value) };
}

export function nativeAgentFormValueAt(
  current: JsonObject,
  path: string[],
): NativeAgentFieldValue {
  let value: unknown = current;
  for (const key of path) {
    if (!isObject(value)) return "";
    value = value[key];
  }
  return typeof value === "string"
    || typeof value === "number"
    || typeof value === "boolean"
    || value === null
    ? value
    : "";
}

function objectDefaults(root: JsonObject, schemaValue: unknown): JsonObject {
  const schema = resolveSchema(root, schemaValue, "schema defaults");
  const properties = requireObject(schema.properties, "schema default properties");
  return Object.fromEntries(Object.entries(properties).map(([key, value]) => {
    const fieldSchema = resolveSchema(root, value, `schema default ${key}`);
    if (Object.prototype.hasOwnProperty.call(fieldSchema, "default")) {
      return [key, cloneJsonValue(fieldSchema.default)];
    }
    if (schemaKind(fieldSchema) === "object") return [key, objectDefaults(root, fieldSchema)];
    if (schemaKind(fieldSchema) === "boolean") return [key, false];
    return [key, ""];
  }));
}

function scalarField(root: JsonObject, schemaValue: unknown, path: string[]): NativeAgentFormField {
  const schema = resolveSchema(root, schemaValue, path.join("."));
  const kind = schemaKind(schema);
  if (kind === "object") throw new Error(`AgentScope schema 字段 ${path.join(".")} 不支持嵌套对象。`);
  return {
    key: path.at(-1) || "field",
    path,
    title: schemaTitle(schema, path.at(-1) || "field"),
    description: optionalString(schema.description),
    kind,
    textarea: schema.format === "textarea",
    minimum: numericBound(schema.minimum, schema.exclusiveMinimum),
    maximum: numericBound(schema.maximum, schema.exclusiveMaximum),
  };
}

function resolveSchema(root: JsonObject, schemaValue: unknown, label: string): JsonObject {
  const schema = requireObject(schemaValue, label);
  if (typeof schema.$ref !== "string") return schema;
  const prefix = "#/$defs/";
  if (!schema.$ref.startsWith(prefix)) throw new Error(`不支持的 AgentScope schema 引用：${schema.$ref}`);
  const definitions = requireObject(root.$defs, "AgentData $defs");
  return requireObject(definitions[schema.$ref.slice(prefix.length)], label);
}

function schemaKind(schema: JsonObject): NativeAgentFormField["kind"] | "object" {
  const direct = schema.type;
  if (["string", "number", "integer", "boolean", "object"].includes(String(direct))) {
    return direct as NativeAgentFormField["kind"] | "object";
  }
  if (Array.isArray(schema.anyOf)) {
    const nonNull = schema.anyOf
      .filter(isObject)
      .find((item) => item.type !== "null");
    if (nonNull) return schemaKind(nonNull);
  }
  throw new Error(`不支持的 AgentScope schema 字段类型：${String(direct || "unknown")}`);
}

function schemaTitle(schema: JsonObject, fallback: string) {
  return optionalString(schema.title) || fallback;
}

function numericBound(inclusive: unknown, exclusive: unknown) {
  if (typeof inclusive === "number") return inclusive;
  if (typeof exclusive === "number") return exclusive;
  return undefined;
}

function optionalString(value: unknown) {
  return typeof value === "string" && value.trim() ? value.trim() : undefined;
}

function cloneJsonValue(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(cloneJsonValue);
  if (isObject(value)) return Object.fromEntries(Object.entries(value).map(([key, item]) => [key, cloneJsonValue(item)]));
  return value;
}

function requireObject(value: unknown, label: string): JsonObject {
  if (!isObject(value)) throw new Error(`${label} 不是 JSON object。`);
  return value;
}

function isObject(value: unknown): value is JsonObject {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
