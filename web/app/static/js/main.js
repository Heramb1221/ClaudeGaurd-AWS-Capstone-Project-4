(function () {
  const dropzone = document.getElementById("dropzone");
  const fileInput = document.getElementById("contract_file");
  const selectedFileLabel = document.getElementById("selected-file");

  if (!dropzone || !fileInput) return;

  function showSelectedFile(file) {
    if (file) {
      selectedFileLabel.textContent = "Selected: " + file.name;
    }
  }

  fileInput.addEventListener("change", () => {
    showSelectedFile(fileInput.files[0]);
  });

  ["dragenter", "dragover"].forEach((eventName) => {
    dropzone.addEventListener(eventName, (e) => {
      e.preventDefault();
      e.stopPropagation();
      dropzone.classList.add("dragover");
    });
  });

  ["dragleave", "drop"].forEach((eventName) => {
    dropzone.addEventListener(eventName, (e) => {
      e.preventDefault();
      e.stopPropagation();
      dropzone.classList.remove("dragover");
    });
  });

  dropzone.addEventListener("drop", (e) => {
    const files = e.dataTransfer.files;
    if (files && files.length > 0) {
      fileInput.files = files;
      showSelectedFile(files[0]);
    }
  });
})();
