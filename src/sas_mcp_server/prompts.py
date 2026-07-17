# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""
Prompt templates for the SAS MCP server.
Registered via ``register_prompts(mcp)`` on the FastMCP instance.
"""

from fastmcp import FastMCP
from fastmcp.prompts import Message


def register_prompts(mcp: FastMCP) -> None:
    """Register all prompt templates on *mcp*."""

    @mcp.prompt()
    def debug_sas_log(log_text: str, severity_filter: str | None = None) -> list[Message]:
        """Analyze a SAS log for errors, warnings, and notes with root-cause explanations and suggested fixes."""
        filter_instruction = ""
        if severity_filter:
            filter_instruction = f"\nFocus only on {severity_filter}-level messages."
        return [Message(role="user", content=(
            f"Analyze the following SAS log output. Identify all errors, warnings, and notes. "
            f"For each issue, explain the root cause and suggest a fix.{filter_instruction}\n\n"
            f"```\n{log_text}\n```"
        ))]

    @mcp.prompt()
    def explore_dataset(library: str, dataset: str,
                        focus_vars: str | None = None) -> list[Message]:
        """Generate comprehensive SAS data-profiling code (CONTENTS, MEANS, FREQ, UNIVARIATE)."""
        vars_instruction = ""
        if focus_vars:
            vars_instruction = f"\nFocus the analysis on these variables: {focus_vars}."
        return [Message(role="user", content=(
            f"Generate SAS code to comprehensively explore the dataset {library}.{dataset}. "
            f"Include PROC CONTENTS, PROC MEANS (for numeric variables), PROC FREQ "
            f"(for categorical variables), and PROC UNIVARIATE (for distribution analysis).{vars_instruction}\n\n"
            f"Make the code production-ready with proper titles and labels."
        ))]

    @mcp.prompt()
    def data_quality_check(library: str, dataset: str,
                           key_variables: str | None = None,
                           business_rules: str | None = None) -> list[Message]:
        """Generate SAS code for a data quality assessment (completeness, uniqueness, validity)."""
        extras = ""
        if key_variables:
            extras += f"\nKey variables to check: {key_variables}."
        if business_rules:
            extras += f"\nBusiness rules to validate: {business_rules}."
        return [Message(role="user", content=(
            f"Generate SAS code to perform a data quality assessment on {library}.{dataset}. "
            f"Check for: completeness (missing values), uniqueness (duplicate keys), "
            f"validity (out-of-range values), and consistency.{extras}\n\n"
            f"Produce a summary report with DQ scores."
        ))]

    @mcp.prompt()
    def statistical_analysis(analysis_type: str, response_variable: str,
                             predictors: str, dataset: str) -> list[Message]:
        """Set up a complete SAS statistical analysis workflow with diagnostics."""
        return [Message(role="user", content=(
            f"Generate SAS code for a {analysis_type} analysis.\n\n"
            f"- Dataset: {dataset}\n"
            f"- Response variable: {response_variable}\n"
            f"- Predictor variables: {predictors}\n\n"
            f"Include: data preparation, model fitting, diagnostic plots, "
            f"assumption checking, and results interpretation comments."
        ))]

    @mcp.prompt()
    def optimize_sas_code(sas_code: str,
                          optimization_focus: str | None = None) -> list[Message]:
        """Review and optimize SAS code for performance, readability, or both."""
        focus = optimization_focus or "performance and readability"
        return [Message(role="user", content=(
            f"Review and optimize the following SAS code. Focus on: {focus}.\n\n"
            f"For each suggestion:\n"
            f"1. Explain what the current code does\n"
            f"2. What the issue is\n"
            f"3. The optimized replacement\n"
            f"4. Expected improvement\n\n"
            f"```sas\n{sas_code}\n```"
        ))]

    @mcp.prompt()
    def explain_sas_code(sas_code: str,
                         audience_level: str | None = None) -> list[Message]:
        """Provide a block-by-block explanation of SAS code, tailored to skill level."""
        level = audience_level or "intermediate"
        return [Message(role="user", content=(
            f"Explain the following SAS code block by block, tailored for a {level}-level "
            f"SAS programmer.\n\n"
            f"For each block:\n"
            f"- What it does\n"
            f"- Key SAS concepts used\n"
            f"- Any potential issues or improvements\n\n"
            f"```sas\n{sas_code}\n```"
        ))]

    @mcp.prompt()
    def sas_macro_builder(macro_name: str, purpose: str,
                          parameters: str | None = None) -> list[Message]:
        """Build a production-quality reusable SAS macro."""
        params_instruction = ""
        if parameters:
            params_instruction = f"\nRequired parameters: {parameters}."
        return [Message(role="user", content=(
            f"Create a production-quality SAS macro named %{macro_name}.\n\n"
            f"Purpose: {purpose}{params_instruction}\n\n"
            f"Requirements:\n"
            f"- Include parameter validation\n"
            f"- Add helpful error messages\n"
            f"- Include a header comment block with usage examples\n"
            f"- Use %LOCAL for all internal macro variables\n"
            f"- Follow SAS macro best practices"
        ))]

    @mcp.prompt()
    def generate_report(dataset: str,
                        report_type: str | None = None,
                        output_format: str | None = None) -> list[Message]:
        """Generate SAS ODS/PROC REPORT code for formatted output."""
        rtype = report_type or "summary"
        fmt = output_format or "HTML"
        return [Message(role="user", content=(
            f"Generate SAS code to create a {rtype} report from the dataset {dataset} "
            f"in {fmt} format.\n\n"
            f"Use ODS destinations and PROC REPORT (or PROC TABULATE as appropriate). "
            f"Include:\n"
            f"- Professional formatting and styling\n"
            f"- Titles and footnotes\n"
            f"- Appropriate summary statistics\n"
            f"- Proper ODS open/close statements"
        ))]

    @mcp.prompt()
    def build_va_dashboard(table: str,
                           audience: str | None = None,
                           focus: str | None = None) -> list[Message]:
        """Build a polished multi-page Visual Analytics dashboard from a CAS table,
        using the report-authoring tools (discover → shape → structure → polish → verify)."""
        audience_line = f"\nAudience: {audience} — match depth and terminology to them." if audience else ""
        focus_line = f"\nFocus / key questions: {focus}." if focus else ""
        return [Message(role="user", content=(
            f"Build a polished SAS Visual Analytics dashboard from the CAS table {table}, "
            f"using the report-authoring tools (describe_report_objects, create_report, "
            f"apply_report_operations, get_report_outline, export_report).{audience_line}{focus_line}\n\n"
            f"Work through these stages in order:\n\n"
            f"1. DISCOVER — get_castable_columns on {table}; classify each column (measure, "
            f"category, date, geography); pick 3-5 headline KPIs; sketch a page plan "
            f"(overview -> detail -> data) BEFORE creating anything. Use "
            f"describe_report_objects()'s intent_map to pick a deliberate chart variety.\n\n"
            f"2. SHAPE THE DATA — in addData, use dataItems to rename every used column to a "
            f"human label, apply formats (DOLLAR/PERCENT/COMMA), set the right aggregation "
            f"(average for rates and prices — never sum a ratio), and classify geography "
            f"columns. See describe_report_objects(operation='addData'). After a rename, "
            f"dataRoles must use the NEW label.\n\n"
            f"3. STRUCTURE — build the skeleton in ONE atomic create_report: pages with titles "
            f"(addPage.title renders as a text band at the top of the page body — page headers "
            f"accept only controls), a KPI row (standardContainer + keyValue tiles), and the "
            f"main visuals. Then chain apply_report_operations calls for side-by-side layout "
            f"via relativeToObject, targeting the object names each result returns (same-batch "
            f"forward references fail; placement is write-once). The page body auto-flows "
            f"VERTICALLY — objects that are merely page-placed render as one tall stack, so "
            f"tiles go side by side in the container and charts pair up left/right.\n\n"
            f"4. POLISH — give every visual a meaningful options.object.title at add time, "
            f"EXCEPT keyValue tiles (they render their measure's label prominently — name the "
            f"measure well in dataItems instead); 3-7 objects per page; pie charts only for "
            f"five or fewer slices; detail rows in a listTable on their own page.\n\n"
            f"5. VERIFY — after each structural change, get_report_outline for the structure "
            f"and export_report (png, one page label at a time — whole-report png can render "
            f"blank) to LOOK at the result; iterate until the overview page answers the key "
            f"questions in five seconds."
        ))]
