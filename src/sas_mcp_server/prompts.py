# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""
Prompt templates for the SAS MCP server.
Registered via ``register_prompts(mcp)`` on the FastMCP instance.
"""

from typing import Optional
from fastmcp.prompts.prompt import Message


def register_prompts(mcp):
    """Register all prompt templates on *mcp*."""

    @mcp.prompt()
    def debug_sas_log(log_text: str, severity_filter: Optional[str] = None) -> list[Message]:
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
                        focus_vars: Optional[str] = None) -> list[Message]:
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
                           key_variables: Optional[str] = None,
                           business_rules: Optional[str] = None) -> list[Message]:
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
                          optimization_focus: Optional[str] = None) -> list[Message]:
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
                         audience_level: Optional[str] = None) -> list[Message]:
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
                          parameters: Optional[str] = None) -> list[Message]:
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
                        report_type: Optional[str] = None,
                        output_format: Optional[str] = None) -> list[Message]:
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
