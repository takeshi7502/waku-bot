/**
 * Select field tests.
 *
 * The point of these is that the control stays a native `<select>`. It was briefly
 * rebuilt as an expanding in-list radio group, which was worse: a settings row that
 * grows several rows when tapped is not what a one-of-many choice looks like. So the
 * first test pins the element itself, and the rest pin that the value round-trips and
 * that the row is still labelled.
 */

import { mount } from "@vue/test-utils";
import { describe, expect, it } from "vitest";

import SelectField from "./SelectField.vue";

const OPTIONS = [
  { value: "zh-CN", text: "τ«ÇΣ╜ôΣ╕¡µûç" },
  { value: "en", text: "English" },
  { value: "ja-JP", text: "µùÑµ£¼Φ¬₧" },
] as const;

function mountField(modelValue = "zh-CN") {
  return mount(SelectField, { props: { modelValue, label: "Φ»¡Φ¿Ç", options: OPTIONS } });
}

describe("SelectField", () => {
  it("renders a native select", () => {
    const wrapper = mountField();

    expect(wrapper.find("select").exists()).toBe(true);
  });

  it("shows the selected option's display name, not its tag", () => {
    const wrapper = mountField();
    const selected = wrapper.get("select").element.selectedOptions[0];

    expect(selected?.textContent?.trim()).toBe("τ«ÇΣ╜ôΣ╕¡µûç");
  });

  it("renders every option", () => {
    const wrapper = mountField();

    expect(wrapper.findAll("option").map((option) => option.text())).toEqual([
      "τ«ÇΣ╜ôΣ╕¡µûç",
      "English",
      "µùÑµ£¼Φ¬₧",
    ]);
  });

  it("emits the chosen value", async () => {
    const wrapper = mountField();

    await wrapper.get("select").setValue("en");

    expect(wrapper.emitted("update:modelValue")).toEqual([["en"]]);
  });

  it("ties the label to the control", () => {
    const wrapper = mountField();

    expect(wrapper.get("label").attributes("for")).toBe(wrapper.get("select").attributes("id"));
  });

  it("shows a hint when one is given", () => {
    const wrapper = mount(SelectField, {
      props: {
        modelValue: "zh-CN",
        label: "Φ»¡Φ¿Ç",
        options: OPTIONS,
        hint: "Θ¥óµ¥┐Σ╕Ä bot σ¢₧σñìτÜäΦ»¡Φ¿Ç",
      },
    });

    expect(wrapper.get("label").text()).toContain("Θ¥óµ¥┐Σ╕Ä bot σ¢₧σñìτÜäΦ»¡Φ¿Ç");
  });

  it("disables the control when asked", () => {
    const wrapper = mount(SelectField, {
      props: { modelValue: "zh-CN", label: "Φ»¡Φ¿Ç", options: OPTIONS, disabled: true },
    });

    expect(wrapper.get("select").attributes("disabled")).toBeDefined();
  });

  it("marks the row when the value is an unsaved edit", () => {
    const wrapper = mount(SelectField, {
      props: { modelValue: "en", label: "Φ»¡Φ¿Ç", options: OPTIONS, changed: true },
    });

    expect(wrapper.get("div").classes()).toContain("border-l-2");
  });
});
